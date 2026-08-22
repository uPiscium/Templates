root := justfile_directory()
runner := root / "tests/runtime/runtime_smoke.py"
debug_runner := root / "tests/runtime/run_opencode_debug.sh"
direct_runner := root / "tests/runtime/run_direct_leaf.sh"

[working-directory: root]
doctor:
    python3 {{quote(runner)}} doctor

[working-directory: root]
prepare issue='issue-41' template='agent-python':
    python3 {{quote(runner)}} prepare --issue {{quote(issue)}} --template {{quote(template)}}

[working-directory: root]
status issue='issue-41':
    python3 {{quote(runner)}} status --issue {{quote(issue)}}

[working-directory: root]
diagnose-child-stall issue='issue-41':
    @echo "Diagnostic control: in the TUI run /task-run SMOKE-CONTROL"
    bash {{quote(debug_runner)}} {{quote(issue)}} child-stall

[working-directory: root]
smoke-depth2 issue='issue-41':
    @echo "Depth-2 Ask smoke: in the TUI run /task-run SMOKE-ASK"
    bash {{quote(debug_runner)}} {{quote(issue)}} depth2-ask

[working-directory: root]
smoke-escalation issue='issue-41':
    @echo "Leaf->Depth-1 escalation release smoke: in the TUI run /task-run SMOKE-ESCALATION"
    bash {{quote(debug_runner)}} {{quote(issue)}} leaf-escalation

[working-directory: root]
smoke-escalation-reject issue='issue-41':
    @echo "Leaf->Depth-1 user-rejection smoke: in the TUI run /task-run SMOKE-ESCALATION-PERMISSION"
    bash {{quote(debug_runner)}} {{quote(issue)}} leaf-escalation-reject

[working-directory: root]
smoke-leaf-completed issue='issue-41':
    @echo "Depth-2 completed leaf smoke: run /task-run SMOKE-LEAF-COMPLETED in the TUI"
    bash {{quote(debug_runner)}} {{quote(issue)}} leaf-completed

[working-directory: root]
smoke-leaf-blocked issue='issue-41':
    @echo "Depth-2 blocked leaf smoke: run /task-run SMOKE-LEAF-BLOCKED in the TUI"
    bash {{quote(debug_runner)}} {{quote(issue)}} leaf-blocked

[working-directory: root]
validate-escalation issue='issue-41':
    python3 {{quote(runner)}} validate-escalation --issue {{quote(issue)}}

[working-directory: root]
validate-leaf-contract issue='issue-41':
    python3 {{quote(runner)}} validate-leaf-contract --issue {{quote(issue)}}

[working-directory: root]
direct-leaf issue='issue-41':
    bash {{quote(direct_runner)}} {{quote(issue)}}

[working-directory: root]
sessions issue='issue-41' label='manual':
    python3 {{quote(runner)}} snapshot-sessions --issue {{quote(issue)}} --label {{quote(label)}}

[working-directory: root]
export-session session issue='issue-41':
    python3 {{quote(runner)}} export-session --issue {{quote(issue)}} --session-id {{quote(session)}}

[working-directory: root]
report issue='issue-41':
    python3 {{quote(runner)}} report --issue {{quote(issue)}}
