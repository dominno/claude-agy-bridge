"""Explicit, local checks of model proposals in fresh scratch trees."""
from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tokenize

from . import config, denylist, runner, safety

TIMEOUT = 300


def read_input(path):
    path = os.path.abspath(path)
    with safety.directory(os.path.dirname(path)) as fd:
        from .stage import _read_regular
        return _read_regular(fd, os.path.basename(path), 8*1024*1024).decode('utf-8')


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
        rel = safety.relative(value)
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
        safety.local(root, source)
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


def scratch(root):
    base = os.environ.get('AGY_BRIDGE_SCRATCH') or os.path.realpath(tempfile.gettempdir())
    absolute = os.path.abspath(base)
    if os.path.commonpath([os.path.realpath(absolute), root]) == root:
        raise ValueError('scratch must be outside the repository')
    with safety.directory(absolute, create=True):
        pass
    return Path(tempfile.mkdtemp(prefix='agy-check-', dir=absolute))


def buffers(root, case):
    return {dest:safety.read(root,safety.local(root,source),8*1024*1024) for dest,source in case['files'].items()}


def build(tree, data):
    tree.mkdir(parents=True)
    for dest, content in data.items():
        path = tree / dest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def subst(text, root, tree):
    return text.replace('{repo}',root).replace('{scratch}',str(tree))


def run_tests(root, case, tree, unsafe_no_sandbox=False):
    argv = [sys.executable if p=='python' else subst(p,root,tree) for p in case['test_cmd']]
    env = {k:subst(v,root,tree) for k,v in case.get('env',{}).items()}
    preflight(root, case, tree, unsafe_no_sandbox)
    return runner.run_scratch(argv,str(tree),root,env,timeout=TIMEOUT,unsafe_no_sandbox=unsafe_no_sandbox)


def preflight(root, case, tree, unsafe_no_sandbox=False):
    if unsafe_no_sandbox:
        return
    command = subst(case['test_cmd'][0], root, tree)
    # Non-Python check commands retain their generic argv interface; their
    # sandbox startup probe uses the bridge's interpreter.
    interpreter = command if os.path.basename(command).lower().startswith('python') else sys.executable
    if interpreter == 'python':
        interpreter = sys.executable
    env = {k:subst(v,root,tree) for k,v in case.get('env',{}).items()}
    runner.sandbox_preflight(interpreter, str(tree), root, env)


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


def grade(root, case, mutants, unsafe_no_sandbox=False):
    validate_case(root,case)
    if not case.get('solution_files'):
        raise ValueError('mutant grading requires solution_files')
    work=scratch(root)
    data=buffers(root,case)
    baseline=work/'reference'
    build(baseline,data)
    rc,detail=run_tests(root,case,baseline,unsafe_no_sandbox)
    (work/'reference.log').write_text(detail)
    if rc:
        raise ValueError('reference failed (exit %d); aborting; evidence: %s' % (rc,work/'reference.log'))
    rows=[]
    for index,m in enumerate(mutants,1):
        row=dict(m,index=index,case=case['name'],job=case.get('job'),status='NOT-APPLICABLE',detail='',reference_pass=True)
        try:
            rel=target(m['file'], root)
            if rel not in case['solution_files']:
                raise ValueError('file is not a named solution target')
            content=data[rel].decode('utf-8')
            if not m['old'] or content.count(m['old'])!=1:
                raise ValueError('old text must be nonempty and occur exactly once')
            if m['old']==m['new']:
                raise ValueError('replacement makes no change')
            replacement=content.replace(m['old'],m['new'],1)
            tree=work/('mutant-%d'%index)
            build(tree,dict(data,**{rel:replacement.encode('utf-8')}))
            rc,tail=run_tests(root,case,tree,unsafe_no_sandbox)
            (work/('mutant-%d.log'%index)).write_text(tail)
            status=('TRIVIAL' if trivial_change(content,replacement) else 'SURVIVES') if rc==0 else 'KILLED'
            row.update(status=status,exit_code=rc,timeout=rc==124,detail=tail,tree=str(tree))
        except (ValueError,OSError) as exc:
            row['detail']=str(exc)
        rows.append(row)
    table,records=work/'table.md',work/'grades.jsonl'
    table.write_text(markdown(rows))
    records.write_text(''.join(json.dumps(r,ensure_ascii=True)+'\n' for r in rows))
    return rows,table,records


def extract_diff(text):
    for label, body in fences(text):
        if label in ('diff', 'patch') or (not label and any(line.startswith(('---', '+++', '@@')) for line in body.splitlines())):
            return body.rstrip('\n')+'\n'
    return None


def check_diff(text, root=None, allowed=None):
    # Accept ordinary unified text diffs only. No links, renames, copies or binary payloads.
    if re.search(r'^(?:GIT binary patch|Binary files|rename |copy |(?:new|old|deleted file|new file) mode 12|index .* 120000)',text,re.M):
        raise ValueError('binary, symlink and rename diffs are refused')
    paths=[]
    in_hunk=False
    for line in text.splitlines():
        if line.startswith(('--- ','+++ ')):
            path=line[4:].split('\t',1)[0]
            if path=='/dev/null':
                continue
            target(path, root)
            paths.append(path)
            in_hunk=False
        elif line.startswith('@@ '):
            if not paths or not re.match(r'^@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@',line):
                raise ValueError('invalid unified hunk header')
            in_hunk=True
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
            continue
        else:
            # patch also understands ed scripts. Never let arbitrary text outside
            # a unified hunk reach that alternative parser/execution surface.
            raise ValueError('only unified text diff syntax is supported')
    if not paths:
        raise ValueError('diff has no unified file headers')
    if allowed is None:
        return (0, 1)
    strips = []
    for strip in (0, 1):
        resolved = [p.split('/', strip)[-1] if p.count('/') >= strip else None for p in paths]
        if all(p in allowed and target(p, root) for p in resolved):
            strips.append(strip)
    if not strips:
        raise ValueError('diff path is not a named solution target (or declared case file when solution_files is omitted)')
    return strips


def apply_diff(root, case, diff, unsafe_no_sandbox=False):
    validate_case(root,case)
    rec=dict(case=case['name'],diff_found=bool(diff),applied=False,how=None,fuzz=False,tests_pass=None)
    if not diff:
        return rec
    strips = check_diff(diff, root, case.get('solution_files') or list(case['files']))
    work=scratch(root)
    data=buffers(root,case)
    diff_file=work/'input.diff'; diff_file.write_text(diff)
    strategies=[('patch -p0',['patch','-f','--verbose','-p0','--no-backup-if-mismatch','-i',str(diff_file)]),
                ('patch -p1',['patch','-f','--verbose','-p1','--no-backup-if-mismatch','-i',str(diff_file)]),
                ('git apply -p0',['git','apply','-p0','--recount',str(diff_file)]),
                ('git apply -p1',['git','apply','-p1','--recount',str(diff_file)])]
    env={'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,'GIT_CEILING_DIRECTORIES':str(work),
         'GIT_TERMINAL_PROMPT':'0'}
    last=''
    for n,(how,argv) in enumerate(strategies):
        tree=work/('strategy-%d'%n)
        build(tree,data)
        preflight(root, case, tree, unsafe_no_sandbox)
        # The patch itself is an input to the sandbox, so keep it in that tree.
        with tempfile.NamedTemporaryFile(prefix='.grader-patch-', dir=tree, delete=False) as patch_input:
            patch_input.write(diff.encode('utf-8'))
        argv = [patch_input.name if value == str(diff_file) else value for value in argv]
        markers = {'fuzz': False, 'offset': False}
        try:
            rc,detail=runner.run_scratch(argv,str(tree),root,env,timeout=TIMEOUT,output_markers=markers,unsafe_no_sandbox=unsafe_no_sandbox,sandbox_step=True)
        except OSError:
            last='strategy executable unavailable'
            continue
        if rc or int(how[-1]) not in strips:
            last=detail[-1000:]
            continue
        rec.update(applied=True,how=how,fuzz=any(markers.values()) or bool(re.search(r'fuzz|offset',detail,re.I)))
        rc,tail=run_tests(root,case,tree,unsafe_no_sandbox)
        rec.update(tests_pass=rc==0,exit_code=rc,timeout=rc==124,scratch=str(tree))
        if rc: rec['test_tail']=tail
        break
    if not rec['applied']: rec['apply_error']=last
    return rec


def diff_table(rows):
    lines=['| Case | Diff found | Applied | Strategy | Fuzz/offset | Tests pass |','| --- | --- | --- | --- | --- | --- |']
    for r in rows:
        lines.append('| '+' | '.join(str(r.get(k,'-')).replace('|','\\|').replace('\n',' ') for k in ('case','diff_found','applied','how','fuzz','tests_pass'))+' |')
    lines.append('\n%d case(s): %d applied, %d pass tests.' % (len(rows),sum(bool(r.get('applied')) for r in rows),sum(bool(r.get('tests_pass')) for r in rows)))
    return '\n'.join(lines)
