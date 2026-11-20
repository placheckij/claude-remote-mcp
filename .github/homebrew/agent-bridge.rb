# The formula placheckij/homebrew-tap serves. Each release (release.yml) sets its url and
# sha256, adds the Python dependencies as resources, and pushes the result to the tap.
class AgentBridge < Formula
  include Language::Python::Virtualenv

  desc "Lets Claude drive the Claude Code agents running in herdr on this Mac"
  homepage "https://github.com/placheckij/claude-remote-mcp"
  url "https://files.pythonhosted.org/packages/d9/39/d149beb5f1b062524c265b0c9ace3b203a8257e2d8bdb02781a56e9e29c9/claude_remote_mcp_agent_bridge-0.2.3.tar.gz"
  sha256 "0f0675fc7d9852d29c8f168e6735cb2ddba8af88d969bad867b7ccac6d93f468"
  license "MIT"

  depends_on "rust" => :build # pydantic-core and watchfiles
  depends_on "libyaml" # pyyaml
  depends_on :macos
  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  # No service block: agent-bridge setup writes the launch agent, so brew services would
  # manage the same job twice.
  def caveats
    <<~EOS
      To install and start the service and enroll this Mac with your gateway, run:
        agent-bridge setup
    EOS
  end

  test do
    assert_equal "agent-bridge #{version}", shell_output("#{bin}/agent-bridge --version").strip
  end
end
