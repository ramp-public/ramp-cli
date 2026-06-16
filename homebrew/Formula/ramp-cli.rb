class RampCli < Formula
  desc "CLI for Ramp's Developer API"
  homepage "https://github.com/ramp-public/ramp-cli"
  version "0.1.5"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-darwin-arm64.tar.gz"
      sha256 "4025483edf1d785d7a700c8d764425a75f99de575fb32a5f4aedf78a0ccb0fd8"
    else
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-darwin-amd64.tar.gz"
      sha256 "c450069b014cf8a1e46b93e2c99414b34d6b0c6e95933e5dbb2ff8790afb60a6"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-linux-arm64.tar.gz"
      sha256 "4d25235f4d1c5a033d494a822f0d015b8692260511da5e7d8f95acbf3ecc57da"
    else
      url "https://github.com/ramp-public/ramp-cli/releases/download/v#{version}/ramp-linux-amd64.tar.gz"
      sha256 "ede24c0e6ea7af6dca558b74e70c8248a2f2c9ac57663b3b670921f7e7ad11c5"
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
