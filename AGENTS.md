# Project Guidelines

## Commit Organization

- Group commits by functionality. Each commit should contain a focused, coherent set of changes that implements or updates one feature, fix, or maintenance task.
- Keep unrelated changes in separate commits, and use commit messages that describe the functionality changed.

## Language

Use English for all user-facing text, UI strings, documentation, source comments, build scripts, and assistant responses for this project. Do not introduce Spanish text.

## Build and Test

- Run the Windows build with `.\build.ps1`.
- Run the test suite with `.\.venv\Scripts\python.exe -m unittest discover -s tests`.
- The `build_app.bat` file is the double-click launcher for the Windows build.
- Set the application version in `app/__init__.py` (`__version__`); the build uses it for versioned output names.
