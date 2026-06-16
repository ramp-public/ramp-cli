#!/usr/bin/env bash
# Update the Homebrew formula with SHA256 checksums from a GitHub Release.
#
# Usage:
#   ./scripts/update-homebrew.sh                  # uses version from __init__.py
#   ./scripts/update-homebrew.sh 0.1.5            # explicit version
#   ./scripts/update-homebrew.sh 0.1.5 --push     # update + push to tap repo
#
# Environment variables:
#   HOMEBREW_TAP_TOKEN   GitHub token with push access to the tap repo (required for --push)
#   HOMEBREW_TAP_REPO    Tap repo (default: ramp-public/homebrew-ramp)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FORMULA="$REPO_ROOT/homebrew/Formula/ramp-cli.rb"
SOURCE_REPO="ramp-public/ramp-cli"
TAP_REPO="${HOMEBREW_TAP_REPO:-ramp-public/homebrew-ramp}"

PUSH=0

# ── Parse arguments ──────────────────────────────────────────

VERSION=""
for arg in "$@"; do
    case "$arg" in
        --push) PUSH=1 ;;
        --help|-h)
            cat <<'EOF'
Update the Homebrew formula with SHA256 checksums from a GitHub Release.

Usage:
    ./scripts/update-homebrew.sh [VERSION] [--push]

Arguments:
    VERSION     Release version without 'v' prefix (e.g. 0.1.5).
                Defaults to the version in src/ramp_cli/__init__.py.

Flags:
    --push      Clone the tap repo and push the updated formula.
                Requires HOMEBREW_TAP_TOKEN with push access.

Environment:
    HOMEBREW_TAP_TOKEN   GitHub PAT with push access to the tap repo.
    HOMEBREW_TAP_REPO    Override tap repo (default: ramp-public/homebrew-ramp).
EOF
            exit 0
            ;;
        *)
            if [ -z "$VERSION" ]; then
                VERSION="$arg"
            else
                echo "Unknown argument: $arg" >&2
                exit 1
            fi
            ;;
    esac
done

# ── Resolve version ──────────────────────────────────────────

if [ -z "$VERSION" ]; then
    VERSION=$(python3 -c "
import re, pathlib
text = pathlib.Path('$REPO_ROOT/src/ramp_cli/__init__.py').read_text()
m = re.search(r'__version__\s*=\s*[\"'']([^\"'']+)[\"'']', text)
print(m.group(1) if m else '')
")
    if [ -z "$VERSION" ]; then
        echo "Could not detect version from __init__.py" >&2
        exit 1
    fi
fi

TAG="v${VERSION}"
echo "Updating formula for version ${VERSION} (tag ${TAG})..."

# ── Fetch SHA256 checksums ────────────────────────────────────

BASE_URL="https://github.com/${SOURCE_REPO}/releases/download/${TAG}"

fetch_sha256() {
    local platform="$1"
    local url="${BASE_URL}/ramp-${platform}.tar.gz.sha256"
    local checksum
    checksum=$(curl -fsSL "$url" 2>/dev/null | awk '{print $1}')
    if [ -z "$checksum" ]; then
        echo "Failed to fetch SHA256 for ${platform} from ${url}" >&2
        exit 1
    fi
    echo "$checksum"
}

echo "Fetching checksums from ${BASE_URL}..."
SHA_DARWIN_ARM64=$(fetch_sha256 "darwin-arm64")
SHA_DARWIN_AMD64=$(fetch_sha256 "darwin-amd64")
SHA_LINUX_ARM64=$(fetch_sha256 "linux-arm64")
SHA_LINUX_AMD64=$(fetch_sha256 "linux-amd64")

echo "  darwin-arm64: ${SHA_DARWIN_ARM64}"
echo "  darwin-amd64: ${SHA_DARWIN_AMD64}"
echo "  linux-arm64:  ${SHA_LINUX_ARM64}"
echo "  linux-amd64:  ${SHA_LINUX_AMD64}"

# ── Generate formula ──────────────────────────────────────────

cat > "$FORMULA" <<RUBY
class RampCli < Formula
  desc "CLI for Ramp's Developer API"
  homepage "https://github.com/ramp-public/ramp-cli"
  version "${VERSION}"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-darwin-arm64.tar.gz"
      sha256 "${SHA_DARWIN_ARM64}"
    else
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-darwin-amd64.tar.gz"
      sha256 "${SHA_DARWIN_AMD64}"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-linux-arm64.tar.gz"
      sha256 "${SHA_LINUX_ARM64}"
    else
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-linux-amd64.tar.gz"
      sha256 "${SHA_LINUX_AMD64}"
    end
  end

  def install
    # The tarball contains main.dist/ with the Nuitka standalone binary and
    # bundled shared libraries. Install everything into libexec/ and symlink
    # the binary into bin/.
    os = OS.mac? ? "darwin" : "linux"
    arch = Hardware::CPU.arm? ? "arm64" : "amd64"
    binary_name = "ramp-#{os}-#{arch}"

    # Find the standalone distribution directory (main.dist/)
    dist_dir = buildpath / "main.dist"
    if dist_dir.exist?
      libexec.install dist_dir.children
    else
      # Fallback: install everything in the current directory
      libexec.install buildpath.children
    end

    bin.install_symlink libexec / binary_name => "ramp"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/ramp --version")
  end
end
RUBY

echo "Formula updated at ${FORMULA}"

# ── Push to tap repo ──────────────────────────────────────────

if [ "$PUSH" -eq 1 ]; then
    if [ -z "${HOMEBREW_TAP_TOKEN:-}" ]; then
        echo "HOMEBREW_TAP_TOKEN is required for --push" >&2
        exit 1
    fi

    TMPDIR=$(mktemp -d)
    trap 'rm -rf "$TMPDIR"' EXIT

    echo "Cloning tap repo ${TAP_REPO}..."
    git clone -q "https://x-access-token:${HOMEBREW_TAP_TOKEN}@github.com/${TAP_REPO}.git" "$TMPDIR/tap"
    cd "$TMPDIR/tap"

    mkdir -p Formula
    cp "$FORMULA" Formula/ramp-cli.rb

    git add Formula/ramp-cli.rb
    if git diff --cached --quiet; then
        echo "Formula is already up to date in tap repo."
    else
        git -c user.name="Ramp" -c user.email="agents@ramp.com" \
            commit -m "ramp-cli ${VERSION}"
        git push origin HEAD
        echo "Pushed formula update to ${TAP_REPO}"
    fi
fi
