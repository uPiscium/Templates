root := justfile_directory()
tool := root / "tools/automation_recovery_bridge.py"

[working-directory: root]
recover-maintenance-authority target:
    python3 -I {{quote(tool)}} recover-maintenance-authority {{quote(target)}}

[working-directory: root]
commit-recovered-maintenance target task message="":
    python3 -I {{quote(tool)}} commit-recovered-maintenance {{quote(target)}} {{quote(task)}} {{quote(message)}}

[working-directory: root]
recover-task-contract-from-issue target issue:
    python3 -I {{quote(tool)}} recover-task-contract-from-issue {{quote(target)}} {{quote(issue)}}

[working-directory: root]
rebind-maintenance-provenance target expected_revision:
    python3 -I {{quote(tool)}} rebind-maintenance-provenance {{quote(target)}} {{quote(expected_revision)}}

[working-directory: root]
resume-contract-check target task:
    python3 -I {{quote(tool)}} resume-contract-check {{quote(target)}} {{quote(task)}}

[working-directory: root]
maintenance-finalize target task pr expected_implementation_revision:
    python="$(command -v python3)"; case "$python" in /nix/store/*/bin/python3|/usr/bin/python3) ;; *) printf '%s\n' 'ERROR: trusted Python interpreter is unavailable' >&2; exit 2 ;; esac; "$python" -I {{quote(tool)}} maintenance-finalize {{quote(target)}} {{quote(task)}} {{quote(pr)}} {{quote(expected_implementation_revision)}}
