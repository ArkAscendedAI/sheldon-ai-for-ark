# Contributing to Sheldon AI for ARK

Thanks for your interest in contributing! This guide will help you get started.

## Ways to Contribute

### Report Bugs
- Open an [issue](https://github.com/ArkAscendedAI/sheldon-ai-for-ark/issues) with reproduction steps
- Include your LLM provider, model, and bridge version

### Submit Data
ARK knowledge base improvements are always welcome:
- Dino nicknames and common names
- Blueprint paths for modded content
- Map location data (coordinates, descriptions)
- Taming/breeding information corrections

### Write Code
- Bug fixes
- New tools (query or action)
- New LLM provider support
- Performance improvements

### Test
- Cross-platform mod testing (Xbox, PS5)
- LLM provider compatibility reports
- Permission enforcement edge cases

## Development Setup

### Bridge (Python)

```bash
git clone https://github.com/ArkAscendedAI/sheldon-ai-for-ark.git
cd sheldon-ai-for-ark/mcp-bridge
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

### Mod (ASA DevKit)

The mod requires the ARK: Survival Ascended DevKit (free via Epic Games Launcher).

## Pull Request Process

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Add or update tests for permission-related changes
5. Run `pytest` to verify all tests pass
6. Commit with clear, descriptive messages
7. Push to your fork and open a PR

### Commit Messages

Use clear, imperative-mood messages:
- `Add Yutyrannus nicknames to vanilla dino data`
- `Fix HMAC validation for empty nonce field`
- `Add OpenAI provider streaming support`

### Code Style

- Python: follow PEP 8, use type hints
- Format with `ruff` or `black`
- Docstrings on all public functions

## Permission-Related Changes

Changes to the permission system (`permissions.py`, `session.py`, tier definitions) require:
- Comprehensive test coverage
- Review by a maintainer
- No regressions in existing permission enforcement tests

## Security Vulnerabilities

If you find a security vulnerability in the permission system, **do not open a public issue**. Email the maintainers directly (see [SECURITY.md](SECURITY.md)) so the fix can be developed before disclosure.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
