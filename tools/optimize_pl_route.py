"""Optimize a checked DR1 routed checkpoint in an isolated copy; never deploy.

The TD command guide documents import_db -> phys_opt -post_route -> fix_hold.
All functional HDL and pin constraints stay byte-identical to the parent run.
Display FIFO constraints are loaded before optimization, with inline selectors.
"""
import argparse
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from build_pl_soc import sha, display_cdc_sdc, verify_timing_clocks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-result', type=Path, required=True)
    parser.add_argument('--td-dir', type=Path, default=Path('D:/Anlogic/TD_Release_2026.1_SP2.200.067'))
    parser.add_argument('--work-root', type=Path, default=Path('E:/fpga_workflow_runs'))
    parser.add_argument('--effort', choices=('high', 'ultra'), default='ultra')
    parser.add_argument('--checkpoint-stage', choices=('routed', 'place'), default='routed')
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()
    parent = json.loads(args.build_result.read_text())
    metadata = copy.deepcopy(parent['integration'])
    if ((args.checkpoint_stage == 'routed' and parent['status'] != 'compiled') or metadata['profile'] != 'display'
            or metadata['clock_plan'] != 'shared-axi'):
        raise ValueError('Requires the checked display-profile routed build')
    project = Path(metadata['project']).parent
    for name, digest in metadata['project_inputs_sha256'].items():
        if sha(project/name.replace('\\', '/')) != digest:
            raise ValueError('Parent build input changed: '+name)
    checkpoint = args.build_result.parent/('products/'+args.checkpoint_stage+'.db')
    if args.checkpoint_stage == 'place':
        # A process can fail during routing after successfully exporting placement.
        # Verify the original clock report and explicit checkpoint-export success.
        verify_timing_clocks((args.build_result.parent/'products/gate-timing.rpt').read_text(), 'shared-axi', 'display')
        parent_log = Path(parent['log']).read_text(errors='replace')
        if 'finish command "export_db '+checkpoint.as_posix()+'"' not in parent_log:
            raise ValueError('Placed checkpoint was not exported successfully')
    checkpoint_sha = sha(checkpoint)
    work = Path(tempfile.mkdtemp(prefix='powerscope-route-opt-', dir=args.work_root))
    target = work/'soc_prj'
    shutil.copytree(project, target)
    sdc_path = target/'src/constraint/timing.sdc'
    sdc = sdc_path.read_text()
    marker = '# Video FIFO:'
    if sdc.count(marker) != 1:
        raise ValueError('Unexpected parent FIFO constraint layout')
    sdc_path.write_text(sdc.split(marker)[0]+display_cdc_sdc(), encoding='ascii')
    metadata['project'] = str(target/'soc_prj.al')
    metadata['project_inputs_sha256'] = {
        name.replace('\\', '/'): sha(target/name.replace('\\', '/'))
        for name in metadata['project_inputs_sha256']}
    metadata['display_cdc_signoff'] = 'FIFO datapath bounds and synchronizer-reset exceptions applied before post-route optimization and final timing.'
    metadata['parent_physical_checkpoint'] = dict(stage=args.checkpoint_stage, path=str(checkpoint), sha256=checkpoint_sha,
        result=str(args.build_result), result_sha256=sha(args.build_result))
    (work/'integration.json').write_text(json.dumps(metadata, indent=2)+'\n')
    products = work/'products'
    products.mkdir()
    q = lambda path: '{'+Path(path).as_posix()+'}'
    script = work/'optimize.tcl'
    lines = [f'cd {q(target)}', 'if {[catch {',
        'import_device dr1_90.db -package DR1M90GEG400 -speed 2',
        f'import_db {q(checkpoint)}', 'clear_sdc -user',
        'read_sdc {src/constraint/timing.sdc}',
        'update_timing -mode manhattan' if args.checkpoint_stage == 'place' else 'update_timing -mode final',
        f'report_abnormal_timing_target -file {q(products/"abnormal-before.rpt")}',
        f'report_timing_ignored -file {q(products/"ignored-before.rpt")}',
        f'report_clock_intersection -file {q(products/"clock-intersection-before.rpt")}',
        'set_param route post_route_opt high',
        'set_param route postrt_opt_extra_round 4', 'commit_param -step route',
        *(['route'] if args.checkpoint_stage == 'place' else []),
        f'phys_opt -post_route -effort {args.effort}', 'fix_hold',
        'update_timing -mode final', f'export_db {q(products/"routed.db")}',
        f'report_area -io_info -file {q(products/"area.rpt")}',
        f'report_timing_summary -file {q(products/"timing.rpt")}',
        f'report_abnormal_timing_target -file {q(products/"timing-abnormal.rpt")}',
        f'report_timing_ignored -file {q(products/"timing-ignored.rpt")}',
        f'report_timing_exception -file {q(products/"timing-exceptions.rpt")}',
        f'check_timing -verbose -file {q(products/"timing-check.rpt")}',
        f'report_clock_utilization -file {q(products/"clock-utilization.rpt")}',
        'report_timing_path -from [get_regs {design_0_inst/powerscope_core/modbus/receiver/rx_meta}] '
        '-to [get_regs {design_0_inst/powerscope_core/modbus/receiver/rx_sync}] '
        f'-file {q(products/"rx-path.rpt")}',
        'commit_param -step bitgen', f'bitgen -bit {q(products/"image.bit")}',
        '} err]} {puts stderr "FLOW_FAILED: $err"; exit 1}',
        'puts "ROUTE_OPTIMIZATION_OK"', 'exit 0', '']
    script.write_text('\n'.join(lines), encoding='ascii')
    result = dict(status='failed', operation='route_placed_checkpoint' if args.checkpoint_stage == 'place' else 'post_route_optimization', hardware_accessed=False,
        device='DR1M90GEG400', source_project=metadata['project'], integration=metadata,
        script_sha256=sha(script), effort=args.effort, log=str(work/'build-console.log'))
    print('Optimizing: '+str(work), flush=True)
    start = time.monotonic()
    try:
        with (work/'build-console.log').open('wb') as log:
            run = subprocess.run([str(args.td_dir/'bin/td_commands_prompt.exe'), script.as_posix()],
                cwd=work, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
        log = (work/'build-console.log').read_text(errors='replace')
        if run.returncode or 'ROUTE_OPTIMIZATION_OK' not in log or 'FLOW_FAILED' in log:
            raise RuntimeError('TD optimization failed with exit code '+str(run.returncode))
        bit = products/'image.bit'
        timing = (products/'timing.rpt').read_text()
        clocks = verify_timing_clocks(timing, 'shared-axi', 'display')
        metrics = {key: float(re.search(pattern, timing)[1]) for key, pattern in {
            'setup_wns_ns': r'SWNS:\s*([-+\d.]+)ns', 'hold_wns_ns': r'HWNS:\s*([-+\d.]+)ns',
            'sta_coverage_percent': r'STA coverage\s*:\s*([\d.]+)%'}.items()}
        result.update(status='compiled', bit=str(bit), bit_sha256=sha(bit), bytes=bit.stat().st_size,
            timing_report=str(products/'timing.rpt'), timing_metrics=metrics, timing_clocks=clocks,
            deployable=False, deployment_review='Requires timing and ignored-path review, then physical acceptance')
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        result['seconds'] = time.monotonic()-start
        (work/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        print('Result: '+str(work/'result.json'), flush=True)


if __name__ == '__main__':
    main()
