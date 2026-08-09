# Repository Policy

Agent-ready repositories carry a declarative GitHub repository policy in `.automation/repository-policy.json`.

The expected default branch is `main`. The managed repository ruleset targets `~DEFAULT_BRANCH` and requires all changes to reach the default branch through a pull request. The ruleset requires zero approving reviews, so a pull request is mandatory while self-merge remains possible. Default-branch deletion and force pushes are also blocked. No bypass actor is configured.

`policy-check` and `policy-apply` require both configured ruleset equality and effective rules on `main` (via GitHub’s branch rules API). A matching configured ruleset alone is insufficient.

Repository policy is separate from project bootstrap and session initialization:

```text
project::bootstrap
  -> project files only

repository::policy-check
  -> read-only GitHub policy comparison

repository::policy-apply
  -> explicit GitHub repository mutation

/init
  -> read-only session validation
```

Check the current repository without changing it:

```sh
just repository::policy-check
```

`policy-check` is `DRIFT` when configured rules differ or when effective rules on `main` do not match. When the effective-rules API returns `required_approving_review_count`, it must be `0`; an omitted parameter is not inferred. If repository visibility or GitHub plan/feature availability limits enforcement, this is reported diagnostically and is still `DRIFT`.

API unavailability, permission issues, or unexpected schema responses are treated as command errors, not as an absence of effective rules.

Apply the policy explicitly:

```sh
just repository::policy-apply
```

`policy-apply` acts only on the GitHub repository resolved from the current checkout. It does not accept an arbitrary repository target. It requires GitHub Administration write permission because it may update the repository default branch and create or update a repository ruleset.

After applying configured changes, `policy-apply` performs final effective verification; it fails if the policy is configured but not effective on `main`.

If the current default branch is not `main`, `policy-apply` changes it only when a `main` branch already exists. It does not create or rename branches automatically. Create or rename `main` explicitly first when adopting an existing repository with another branch layout.

Repository policy mutation is refused from a Task worktree. In OpenCode, `repository::policy-check` is read-only and allowed; `repository::policy-apply` requires Ask.
