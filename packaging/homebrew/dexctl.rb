class Dexctl < Formula
  include Language::Python::Virtualenv

  desc "Codex account control plane"
  homepage "https://github.com/ravihammond/dexctl"
  url "https://github.com/ravihammond/dexctl/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "5582265572859f6fa3ac9d96f8fb01cd81d1ecc712bfbbf9d581e5506b63dd39"
  license "MIT"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "dexctl", shell_output("#{bin}/dexctl --help")
  end
end
