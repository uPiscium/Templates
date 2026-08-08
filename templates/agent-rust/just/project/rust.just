root := justfile_directory()

[working-directory: root]
format:
    cargo fmt --all

[working-directory: root]
add crate:
    cargo add {{quote(crate)}}
