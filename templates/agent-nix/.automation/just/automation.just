root := justfile_directory() / ".." / ".."
script := root / ".automation" / "bin" / "automation_upgrade.py"

[working-directory: root]
version:
    python3 {{quote(script)}} version

[working-directory: root]
check-update source:
    python3 {{quote(script)}} check-update --source {{quote(source)}}

[working-directory: root]
upgrade source:
    python3 {{quote(script)}} upgrade --source {{quote(source)}}

[working-directory: root]
bootstrap-receipt source:
    python3 {{quote(script)}} bootstrap-receipt --source {{quote(source)}}

[working-directory: root]
commit task message="":
    python3 {{quote(script)}} commit {{quote(task)}} {{quote(message)}}
