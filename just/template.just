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
