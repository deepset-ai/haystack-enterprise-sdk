#!/usr/bin/env bash
# Install the haystack-enterprise CLI:
#   curl -fsSL https://raw.githubusercontent.com/deepset-ai/haystack-enterprise-sdk/main/install.sh | bash
#
# Optional environment variables:
#   HE_VERSION  install a specific version, e.g. HE_VERSION=0.2.0
#   HE_EXTRAS   install extras, e.g. HE_EXTRAS="[deploy]"
set -euo pipefail

PACKAGE="haystack-enterprise-sdk${HE_EXTRAS:-}${HE_VERSION:+==$HE_VERSION}"
DOCS_URL="https://docs.cloud.deepset.ai/reference/sdk-overview"

case "$(uname -s)" in
  Linux | Darwin) ;;
  *)
    echo "❌ This installer supports macOS and Linux. On Windows, run: uv tool install haystack-enterprise-sdk" >&2
    exit 1
    ;;
esac

cat <<'EOF'
🥁 Hello and welcome! Now installing the...
  _   _                 _             _
 | | | | __ _ _   _ ___| |_ __ _  ___| | __
 | |_| |/ _` | | | / __| __/ _` |/ __| |/ /
 |  _  | (_| | |_| \__ \ || (_| | (__|   <
 |_| |_|\__,_|\__, |___/\__\__,_|\___|_|\_\
              |___/        E N T E R P R I S E   C L I

EOF

echo "🔍 Checking for uv..."
if ! command -v uv >/dev/null 2>&1; then
  echo "💾 uv not found. Installing it from https://astral.sh/uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "✅ Using $(uv --version)"

echo "📦 Installing ${PACKAGE}..."
uv tool install --quiet --upgrade --python 3.12 "$PACKAGE"

echo "🔗 Making sure the uv tool directory is on your PATH..."
uv tool update-shell >/dev/null 2>&1 || true

bin_dir="$(uv tool dir --bin)"
version="$("$bin_dir/haystack-enterprise" --version)"

cat <<EOF

📺 Success! ${version} is now installed!
🔐 Next, log in with:       haystack-enterprise login
🚀 Deploy a pipeline with:  haystack-enterprise deploy pipeline.py SERVICE
📚 Docs:                    ${DOCS_URL}
EOF

if ! command -v haystack-enterprise >/dev/null 2>&1; then
  echo
  echo "↻  Open a new terminal (or add ${bin_dir} to your PATH) to use haystack-enterprise."
fi
