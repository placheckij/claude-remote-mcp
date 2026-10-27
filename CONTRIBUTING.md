# Contributing

Thanks for your interest. This project lets a remote caller drive Claude Code agents on
someone's computer, so changes are reviewed closely and pull requests are **by
invitation**: only the people listed below can open them.

## Everyone

- **Found a bug or have an idea?** Open an issue. Describe what you did, what happened
  and what you expected; for a change, what problem it solves.
- **Found a security problem?** Don't open an issue; follow [SECURITY.md](SECURITY.md).
- **Want to contribute code?** Open an issue describing the change first. If it fits,
  you may be invited as a collaborator.

## Invited contributors

- Work on a branch and open a pull request against `main`. Keep each one to one change.
- Every pull request needs the maintainer's approval and passing CI (tests on Linux and
  macOS, `shellcheck` on the setup scripts). New commits after an approval need a new
  one.
- Add or update tests with the change: a bug fix comes with a test that fails without it.
- Set up and run the tests as in the README's [Development](README.md#development)
  section.
- Tool output is read aloud to someone who may be driving: keep replies short and
  speakable, and refusals plain enough to act on.
- Never commit tokens, personal paths or LAN addresses; examples use `/Users/me`,
  `192.168.1.20` and `mcp.example.com`.

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE), like the rest of the project.

## Approved contributors

People who can open pull requests. They are also the repository's collaborators; to
add someone, invite them under Settings → Collaborators and add them here.

| GitHub | Role |
|---|---|
| [@placheckij](https://github.com/placheckij) | maintainer |
