# FM-1 Firmware Tool

A Windows app for installing a locally saved `.fwsc` firmware package when the official web installer cannot complete the update.

## Quick start

You need Windows, Python 3.11 or later, and an FM-1 USB data cable. Keep the FM-1 disconnected while installing the dependencies.

From the project folder, open PowerShell and run:

```powershell
py -3 -m pip install -r motor\requirements-lock.txt
```

Then double-click [`run_gui.bat`](run_gui.bat). Choose your `.fwsc` file, connect the FM-1, click **Detect FM-1**, and follow the prompts.

Do not disconnect or power off the FM-1 while firmware is being written. If the app displays a safety warning, follow it before doing anything else.

The app keeps its existing data folder, `Documents\FM1-Installer`, for backups and logs so previously saved files remain available. Backups contain device data; do not share them publicly.

## Console use

Open Command Prompt or PowerShell in the project folder. For installation, run the console as administrator:

```powershell
.\fm1.bat install "C:\path\to\firmware.fwsc"
.\fm1.bat check "C:\path\to\firmware.fwsc"
.\fm1.bat detect
```

`check` inspects the package and local setup without accessing the FM-1. For more help, run `.\fm1.bat install --help`.

## More information

See [Technical details](TECHNICAL_DETAILS.md) for recovery and safety checks, simulation, troubleshooting, and development or build instructions.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

Third-party components retain their own license notices in `motor/`.

## AI-Assisted Development

This project was developed extensively using AI-assisted coding tools, including **ChatGPT, OpenAI Codex, Anthropic Claude, GitHub Copilot, OpenCode, and DeepSeek**, integrated into **Visual Studio Code**.

AI was primarily responsible for generating source code, implementation details, and documentation, under human direction and supervision.
