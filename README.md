# 🚀 Cloudsmith CENG Template

A reusable template repository maintained by the **Customer Engineering (CENG)** team at Cloudsmith.  
This repo is intended to accelerate the development of examples, scripts, integrations, and demo workflows that help customers use Cloudsmith more effectively.

---

## 📦 What’s Inside

- GitHub Issue Forms for bugs and feature requests
- CI/CD example workflow (Python-based)
- Contribution and pull request templates
- Environment variable and code linting examples
- Directory structure for `src/` and `tests/`

---

## 📁 Structure

```
.
├── .github/                        # GitHub-specific automation and templates
│   ├── ISSUE_TEMPLATE/             # Issue forms using GitHub Issue Forms
│   │   ├── bug_report.yml          # Form for reporting bugs
│   │   └── feature_request.yml     # Form for suggesting features
│   ├── workflows/                  # GitHub Actions workflows (e.g., CI pipelines)
│   ├── PULL_REQUEST_TEMPLATE.md    # Template used when creating pull requests
│   └── CODEOWNERS                  # Defines reviewers for specific paths
├── src/                            # Scripts, API integrations, or example tools
├── tests/                          # Tests for scripts and tools in src/
├── .env.example                    # Sample environment config (e.g., API keys)
├── .gitignore                      # Ignore rules for Git-tracked files
├── .editorconfig                   # Code style config to ensure consistency across IDEs
├── CHANGELOG.md                    # Log of project changes and version history
├── CONTRIBUTING.md                 # Guidelines and checklists for contributors
├── LICENSE                         # Licensing information (Apache 2.0)
└── README.md                       # This file
```

---

## 🛠 Getting Started

1. Clone the template:
   ```bash
   git clone https://github.com/cloudsmith-examples/ceng-template.git
   cd ceng-template
   ```

2. Install any dependencies or activate your environment.

3. Start building your example in the `src/` directory.

4. Use the `.env.example` as a guide for credentials if needed.

---

## 🧩 Use Cases

- Building and testing Cloudsmith integrations for CI/CD platforms
- Creating reproducible customer issue examples
- Building Cloudsmith CLI or API automations
- Prototyping workflows for CI/CD platforms

---

## 🤝 Contributing

Want to contribute? Please see [`CONTRIBUTING.md`](CONTRIBUTING.md).

Maintainers and reviewers are listed in [`CODEOWNERS`](.github/CODEOWNERS).

---

## 📄 License

Licensed under the [Apache 2.0 License](LICENSE).
