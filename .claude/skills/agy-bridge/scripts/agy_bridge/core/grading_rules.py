"""Rules for model proposals: mutant rows, case files and unified diffs. Pure checks; nothing is run here."""
from __future__ import annotations

import ast
import io
import json
import re
import tokenize

from . import config, denylist, repo_paths


def unannotate(text):
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith(('UNVERIFIED (', 'VERIFIED (')):
            _, marker, rest = line.partition('): ')
            if marker:
                line = rest
        lines.append(line)
    return ''.join(lines)


def fences(text):
    """Consume each line once; unmatched/repeated openers are never rescanned."""
    label, body = None, []
    for line in unannotate(text).splitlines():
        if label is None:
            if line.startswith('```'):
                label, body = line[3:].strip(), []
        elif line.rstrip(' \t') == '```':
            yield label, '\n'.join(body)
            label, body = None, []
        else:
            body.append(line)


def extract_mutants(text):
    blocks = [body for label, body in fences(text) if label == 'mutants']
    if not blocks:
        raise ValueError('no fenced mutants block')
    rows = []
    for block in blocks:
        for line in block.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (not isinstance(row, dict) or set(row) != {'file', 'old', 'new', 'why'} or
                    not all(isinstance(v, str) for v in row.values())):
                raise ValueError('each mutant needs exactly string file, old, new, why fields')
            rows.append(row)
    if not rows:
        raise ValueError('empty mutants block')
    return rows


def target(value, root=None):
    try:
        rel = repo_paths.relative(value)
    except config.UsageError as exc:
        raise ValueError(str(exc)) from None
    if denylist.is_denied(rel, config.load_extra_deny(root) if root else ()) or any(ord(c)<32 or ord(c)==127 for c in rel):
        raise ValueError('unsafe scratch target')
    return rel


def validate_case(root, case):
    if not isinstance(case, dict):
        raise ValueError('case must be an object')
    allowed = {'name', 'job', 'files', 'solution_files', 'test_cmd', 'env', 'expect'}
    if set(case)-allowed:
        raise ValueError('unknown case fields')
    if not isinstance(case.get('name'), str) or not case['name'].strip():
        raise ValueError('case needs a name')
    files = case.get('files')
    if not isinstance(files, dict) or not files or len(files)>500:
        raise ValueError('case needs files {destination: repository source}')
    for dest, source in files.items():
        target(dest, root)
        repo_paths.local(root, source)
    keys = sorted(files)
    if any(b.startswith(a+'/') for a,b in zip(keys,keys[1:])):
        raise ValueError('case file destinations overlap')
    cmd = case.get('test_cmd')
    if not isinstance(cmd, list) or not cmd or not all(isinstance(s,str) and s and '\0' not in s for s in cmd):
        raise ValueError('test_cmd must be a nonempty argv list, never shell text')
    env = case.get('env', {})
    if not isinstance(env, dict) or not all(isinstance(k,str) and k and '=' not in k and '\0' not in k and isinstance(v,str) and '\0' not in v for k,v in env.items()):
        raise ValueError('env must map valid names to strings')
    if case.get('expect', 'pass') not in ('pass','fail'):
        raise ValueError('expect must be pass or fail')
    solutions = case.get('solution_files', [])
    if not isinstance(solutions, list):
        raise ValueError('solution_files must be a list')
    for p in solutions:
        if target(p, root) not in files:
            raise ValueError('solution target is absent from case files')
    return case


def trivial_change(before, after):
    """Python tokens plus AST: comments/spacing/docstrings, not syntax rewrites."""
    def normalized(text):
        tree=ast.parse(text)
        docstrings=[]
        for node in ast.walk(tree):
            if isinstance(node,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                if node.body and isinstance(node.body[0],ast.Expr) and isinstance(node.body[0].value,ast.Constant) and isinstance(node.body[0].value.value,str):
                    expr=node.body.pop(0)
                    docstrings.append((expr.lineno,expr.col_offset,expr.end_lineno,expr.end_col_offset))
        ignored={tokenize.COMMENT,tokenize.NL,tokenize.NEWLINE,tokenize.INDENT,tokenize.DEDENT,tokenize.ENDMARKER}
        tokens=[]
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in ignored or any((a,b)<=tok.start and tok.end<=(c,d) for a,b,c,d in docstrings):
                continue
            tokens.append((tok.type,tok.string))
        return ast.dump(tree,include_attributes=False), tokens
    try:
        return normalized(before)==normalized(after)
    except (SyntaxError,ValueError,tokenize.TokenError,IndentationError):
        return False


def markdown(rows):
    def cell(value):
        return str(value).replace('|','\\|').replace('\n','<br>')
    lines=['# Mutant grading','','Reference: PASS. SURVIVES requires semantic review.','',
           '| # | Case | File | Status | Why | Detail |','| --- | --- | --- | --- | --- | --- |']
    for row in rows:
        lines.append('| '+' | '.join(cell(row[k]) for k in ('index','case','file','status','why','detail'))+' |')
    return '\n'.join(lines)+'\n'


def extract_diff(text):
    for label, body in fences(text):
        if label in ('diff', 'patch') or (not label and any(line.startswith(('---', '+++', '@@')) for line in body.splitlines())):
            return body.rstrip('\n')+'\n'
    return None


_HIDDEN_HEADER=re.compile(r'(?:--- |\+\+\+ |\*\*\* |@@ |Index:|diff |[0-9]+(?:,[0-9]+)?[acdi]$|\.$|[sw]/)')


def check_diff(text, root=None, allowed=None):
    # Accept ordinary unified text diffs only. No links, renames, copies or binary payloads.
    if re.search(r'^(?:GIT binary patch|Binary files|rename |copy |(?:new|old|deleted file|new file) mode 12|index .* 120000)',text,re.M):
        raise ValueError('binary, symlink and rename diffs are refused')
    paths=_diff_paths(text, root)
    if not paths:
        raise ValueError('diff has no unified file headers')
    if allowed is None:
        return (0, 1)
    return _strips(paths, root, allowed)


def _diff_paths(text, root):
    """Every file header path, checked line by line; refuses anything that is not unified diff syntax."""
    paths=[]
    in_hunk=False
    old_left=new_left=0
    for line in text.splitlines():
        if line.startswith(('--- ','+++ ')):
            path=line[4:].split('\t',1)[0]
            if path=='/dev/null':
                continue
            target(path, root)
            paths.append(path)
            in_hunk=False
        elif line.startswith('@@ '):
            counts=re.match(r'^@@ -[0-9]+(?:,([0-9]+))? \+[0-9]+(?:,([0-9]+))? @@',line)
            if not paths or not counts:
                raise ValueError('invalid unified hunk header')
            in_hunk=True
            old_left=int(counts[1]) if counts[1] is not None else 1
            new_left=int(counts[2]) if counts[2] is not None else 1
        elif line.startswith('Index: '):
            path = line[len('Index: '):].strip()
            target(path, root)
            paths.append(path)
        elif line.startswith('diff --git '):
            values=line[len('diff --git '):].split()
            if len(values)!=2:
                raise ValueError('quoted or ambiguous diff paths are unsupported')
            for p in values:
                target(p, root)
                paths.append(p)
            in_hunk=False
        elif re.fullmatch(r'(?:new file|deleted file|old|new) mode 100(?:644|755)',line):
            in_hunk=False
        elif re.fullmatch(r'index [0-9a-fA-F]+\.\.[0-9a-fA-F]+(?: 100(?:644|755))?',line):
            in_hunk=False
        elif not line or re.fullmatch(r'=+',line):
            continue
        elif in_hunk and (line.startswith((' ', '+', '-')) or line == r'\ No newline at end of file'):
            if line.startswith(' '):
                old_left-=1; new_left-=1
            elif line.startswith('-'):
                old_left-=1
            elif line.startswith('+'):
                new_left-=1
            if old_left<0 or new_left<0:
                # Beyond the declared counts patch looks for the next file header and
                # strips leading whitespace (indented diffs), while git apply --recount
                # keeps reading the hunk. Tolerate miscounted hunks, but never a line
                # that either parser could read as a header or an ed command.
                if _HIDDEN_HEADER.match(line[1:].lstrip()):
                    raise ValueError('indented diff header or ed command after a hunk is refused')
            continue
        else:
            # patch also understands ed scripts. Never let arbitrary text outside
            # a unified hunk reach that alternative parser/execution surface.
            raise ValueError('only unified text diff syntax is supported')
    return paths


def _strips(paths, root, allowed):
    """The -p levels (0, 1) at which every diff path is an allowed target."""
    strips = []
    for strip in (0, 1):
        resolved = [p.split('/', strip)[-1] if p.count('/') >= strip else None for p in paths]
        if all(p in allowed and target(p, root) for p in resolved):
            strips.append(strip)
    if not strips:
        raise ValueError('diff path is not a named solution target (or declared case file when solution_files is omitted)')
    return strips


def diff_table(rows):
    lines=['| Case | Diff found | Applied | Strategy | Fuzz/offset | Tests pass |','| --- | --- | --- | --- | --- | --- |']
    for r in rows:
        lines.append('| '+' | '.join(str(r.get(k,'-')).replace('|','\\|').replace('\n',' ') for k in ('case','diff_found','applied','how','fuzz','tests_pass'))+' |')
    lines.append('\n%d case(s): %d applied, %d pass tests.' % (len(rows),sum(bool(r.get('applied')) for r in rows),sum(bool(r.get('tests_pass')) for r in rows)))
    return '\n'.join(lines)
