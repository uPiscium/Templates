## Base Project Adapter initialization

- `just project::doctor` must complete successfully before implementation begins.
- This base adapter has no language-specific bootstrap, compiler, package manager, or service prerequisites.
- Do not infer skipped build/lint/test operations as failures; the adapter's public Just recipes report their status explicitly.
