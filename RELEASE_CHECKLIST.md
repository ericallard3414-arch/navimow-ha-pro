# First GitHub release checklist

> GitHub owner is configured as `@ericallard3414-arch` and the repository URL is set to `https://github.com/ericallard3414-arch/navimow-ha-pro`.

1. Create a **public** GitHub repository, suggested name: `navimow-ha-pro`.
2. Upload the contents of this folder to the repository root (not the outer ZIP folder).
3. Add a short GitHub repository description, for example:
   `Unofficial Segway Navimow integration and interactive dashboard for Home Assistant.`
4. Add repository topics such as: `home-assistant`, `hacs`, `navimow`, `segway`,
   `robot-mower`, `lawn-mower`.
5. Review the MIT license and replace the copyright holder if desired.
6. Optionally add your GitHub username to `codeowners` and your repository URL to
   `documentation` in `custom_components/navimow_ha_pro/manifest.json`.
7. Create a GitHub release/tag named `v0.7.0-beta.1` and mark it **Pre-release**.
8. Test a clean HACS custom-repository install on a second Home Assistant instance if
   available.
9. Open the integration page after restart and confirm the local Navimow brand icon is
   displayed (Home Assistant 2026.3+).
10. Invite testers to use the model compatibility issue template.

Before every release, search the repository for personal serial numbers, coordinates,
tokens, passwords, IP addresses and debug captures.
