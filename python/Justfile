# デフォルトのアクション（引数なしで実行された場合）
# エージェントが迷ったときや、コミット前に全体を検証するための安全なフォールバックです。
default: check-all

# =============================================================================
# Agent API: 以下のコマンド群のみを CLAUDE.md でエージェントに露出させます
# =============================================================================

# コードの静的解析（Lint）を実行します
lint target="src/":
    @echo "==> Running Linter (Ruff) on {{target}}..."
    uv run ruff check {{target}}

# コードの自動フォーマットを実行します
format target="src/":
    @echo "==> Running Formatter (Ruff) on {{target}}..."
    uv run ruff format {{target}}
    uv run ruff check --fix {{target}}

# 静的型チェックを実行します
typecheck target="src/":
    @echo "==> Running Type Checker (Mypy) on {{target}}..."
    uv run mypy {{target}}

# テストを実行します
test target="tests/":
    @echo "==> Running Tests (Pytest) on {{target}}..."
    uv run pytest {{target}} -v

# =============================================================================
# 複合タスク (Pipelines)
# =============================================================================

# プルリクエスト作成前や、大きな変更の後にエージェントに実行させる一括検証
check-all: lint typecheck test
    @echo "==> [OK] All checks passed successfully."

# =============================================================================
# ユーティリティ (依存関係管理など)
# =============================================================================

# 依存関係の同期（.envrc からも呼ばれますが、明示的なAPIとしても提供）
sync:
    @echo "==> Syncing dependencies with uv..."
    uv sync

