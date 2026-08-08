root := justfile_directory()

[working-directory: root]
render template:
    python3 tools/render_templates.py render {{quote(template)}}

[working-directory: root]
render-all:
    python3 tools/render_templates.py render-all

[working-directory: root]
check:
    python3 tools/render_templates.py check

[working-directory: root]
adopt-plan target adapter='auto':
    python3 tools/adopt_repository.py plan {{quote(target)}} --adapter {{quote(adapter)}}

[working-directory: root]
adopt-apply target adapter='auto':
    python3 tools/adopt_repository.py apply {{quote(target)}} --adapter {{quote(adapter)}}

[working-directory: root]
adapter-migrate-plan target adapter:
    python3 tools/adapter_migration.py {{quote(target)}} --adapter {{quote(adapter)}}
