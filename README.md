# Elegant R240 Home Assistant Integration

Custom Home Assistant integration for the Elegant R240 bridge.

## Features

- Local communication with the R240 bridge
- Light entities for Elegant LED controllers
- Configuration through Home Assistant UI
- HACS installation support

## Installation via HACS

1. Open HACS.
2. Open the three-dot menu.
3. Select Custom repositories.
4. Add this repository URL.
5. Select type: Integration.
6. Install Elegant R240.
7. Restart Home Assistant.
8. Go to Settings → Devices & services → Add integration.
9. Search for Elegant R240.

## Manual installation

Copy `custom_components/elegant_r240` to:

```text
/config/custom_components/elegant_r240