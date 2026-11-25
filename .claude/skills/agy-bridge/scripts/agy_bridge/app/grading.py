"""Explicit, local checks of model proposals in fresh scratch trees (grade-mutants, apply-diff)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

from ..core.grading_rules import check_diff, target, trivial_change, markdown, validate_case
from ..port import grading_sandbox, safety

TIMEOUT = 300


def read_input(path):
    path = os.path.abspath(path)
    with safety.directory(os.path.dirname(path)) as fd:
        from ..core.stage import _read_regular
        return _read_regular(fd, os.path.basename(path), 8*1024*1024).decode('utf-8')


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
    return grading_sandbox.use().run_scratch(argv,str(tree),root,env,timeout=TIMEOUT,unsafe_no_sandbox=unsafe_no_sandbox)


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
    grading_sandbox.use().sandbox_preflight(interpreter, str(tree), root, env)


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
            rc,detail=grading_sandbox.use().run_scratch(argv,str(tree),root,env,timeout=TIMEOUT,output_markers=markers,unsafe_no_sandbox=unsafe_no_sandbox,sandbox_step=True)
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
