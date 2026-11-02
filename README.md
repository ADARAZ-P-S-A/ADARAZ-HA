# ADARAZ for Home Assistant

Home Assistant integration for ADARAZ devices. The first device is the
**RAZ-RCP-1** card reader / time clock.

The device connects to Home Assistant by itself, over a WebSocket at
`/api/adaraz/ws`. It does not need an open port, and Home Assistant never
connects to the device. Every connection is an encrypted
[Noise](https://noiseprotocol.org/) session in which both sides prove who they
are with keys exchanged at pairing. Plain `http://` inside your LAN is safe, and
`https://` works too.

Repository layout:

| Path | Content |
|---|---|
| `custom_components/adaraz` | the Home Assistant integration |
| `lib` | the `adaraz` Python library (protocol, server, simulated device) |
| `docs/protocol.md` | the ADARAZ Link protocol specification |
| `examples/server.py` | a standalone server without Home Assistant |

## Installation

### HACS (custom repository)

1. In HACS, open the menu, choose **Custom repositories** and add this
   repository with the type **Integration**.
2. Install **ADARAZ** and restart Home Assistant.

### Manual

Copy `custom_components/adaraz` into the `custom_components` folder of your
Home Assistant configuration and restart Home Assistant.

The integration needs the `adaraz` Python package
([PyPI](https://pypi.org/project/adaraz/)), which Home Assistant installs
automatically from the version pinned in `manifest.json`.

Home Assistant 2025.1 or newer is required.

## Pairing a reader

1. Make sure Home Assistant knows its own local address: **Settings > System >
   Network > Home Assistant URL > Local network**. The reader connects to this
   URL, so it must be reachable from the reader (for example
   `http://192.168.1.10:8123`).
2. Go to **Settings > Devices & services > Add integration** and choose
   **ADARAZ**.
3. Home Assistant shows the server URL and a one-time pairing code of 12
   characters such as `7KQ2-MX9T-DA4P` (shown as `XXXX-XXXX-XXXX`). The code is
   valid for 10 minutes, for one device.
4. Open the reader's local admin panel, go to **Settings > Integrations > Home
   Assistant and other servers** and enter the URL and the code. Letter case, the dashes and the
   letters O / I / L (read as 0 / 1 / 1) do not matter.
5. The reader pairs and connects. Home Assistant continues by itself and adds
   the device. If you close the dialog before that, the reader is unpaired
   again.

If the code expires, or a wrong code is entered 5 times, Home Assistant shows
an error and offers a new code. Pairing a reader that is already configured
(for example after a factory reset of the reader) updates the existing entry.

Punches stored on the reader before pairing are not imported. Punches made
while Home Assistant was unreachable are delivered when the reader reconnects,
marked as late.

## Entities

| Entity | Type | Description |
|---|---|---|
| Punch | event | Fires for every card punch. Attributes: `uid` (card UID, hex), `ts` (Unix time of the punch), `late`. |
| Connection | binary sensor (connectivity) | On while the reader is connected to Home Assistant. |
| Card reader | binary sensor (problem) | On when the reader reports that its card reader does not work. |
| Open | button | Pulses the relay according to the reader's relay settings, like an accepted card. |
| Relay | switch | Turns the relay on and off. Its state comes from the reader. |

**Open** and **Relay** exist only when remote relay control is enabled on the
reader. That setting is only available in the reader's local admin panel, for
an administrator. When it changes, Home Assistant adds or removes the two
entities automatically. They are unavailable while the reader is disconnected.

By default an accepted card also opens the relay on the reader itself, without
Home Assistant. To let only Home Assistant decide, choose a relay mode with
**remote management** on the reader (firmware 108 or newer): a card then only
records the punch, and the relay moves only on **Open** / **Relay** (and the
panel's test buttons). Without Home Assistant or the network, nobody gets in
by card in that mode.

### Automations with punches

Use the **Punch** event entity. Its state is the time of the last punch, so
every punch is a state change; the punch itself is in the attributes of the
new state (`uid`, `ts`, `late`). Trigger on the entity's state and check the
attributes of `trigger.to_state`, not the entity's current state (a second
punch may already have replaced it):

```yaml
automation:
  - alias: Open the door for a known card
    triggers:
      - trigger: state
        entity_id: event.wejscie_punch
        not_from: unavailable
        not_to:
          - unavailable
          - unknown
    conditions:
      - condition: template
        value_template: >-
          {{ trigger.to_state.attributes.uid == '04A1B2C3'
             and not trigger.to_state.attributes.late }}
    actions:
      - action: button.press
        target:
          entity_id: button.wejscie_open
```

`not_to` skips the entity becoming unavailable (while the entry reloads), and
`not_from` skips its last state being restored afterwards: neither is a punch.
In the automation editor: trigger **Entity > State** on the Punch entity,
condition **Template**.

A punch is **late** when it reached Home Assistant more than 5 minutes after it
happened (the reader was offline). Keep late punches for history, but do not
use them for real-time actions such as opening a door, as above.

Every punch is delivered once: the reader keeps its log, Home Assistant stores
its position in the log before it confirms a batch (and confirms nothing while
it is shutting down or cannot write its storage), and duplicates (the same
card at the same second) are filtered. When the reader rewrites its log (for
example after records were deleted), it sends the whole log again; punches up
to the newest one Home Assistant had before are then treated as history and
not reported again.

## Removing a reader

Delete the device's entry in **Settings > Devices & services > ADARAZ**. If the
reader is connected, it is told to delete its pairing and stops connecting. If
it is offline, Home Assistant forgets it and refuses its connections; clear the
pairing in the reader's admin panel (or pair it again).

## Troubleshooting

- **The reader does not connect:** check that the URL from step 1 opens in a
  browser on the same network as the reader, and that a firewall does not block
  the port.
- **Behind a reverse proxy:** the proxy must forward WebSocket connections for
  `/api/adaraz/ws`.
- **Diagnostics:** the entry's menu has **Download diagnostics** (keys are
  redacted). Debug logging: enable it for the integration, or set the loggers
  `custom_components.adaraz` and `adaraz` to `debug`.

## Before release

- **Library releases.** `lib/` is published to PyPI as `adaraz` by
  `.github/workflows/release.yml` (Trusted Publishing) when a tag `vX.Y.Z`
  matching `version` in `lib/pyproject.toml` is pushed. Bump the version in
  `lib/pyproject.toml`, `lib/src/adaraz/__init__.py` and the requirement in
  `custom_components/adaraz/manifest.json` together; PyPI never accepts the
  same version twice.
- **Custom build vs. core.** The manifest's `version` and `issue_tracker` keys
  are for the custom build (HACS) and are removed for a core submission. The
  documentation link points to this README until there is a page on
  home-assistant.io. The manifest declares no quality scale until every Bronze
  rule in `quality_scale.yaml` is done (brands: the images in `brands/adaraz`
  go to home-assistant/brands with the core PR).

## Development

```
pip install -e ./lib -r requirements_test.txt
pytest
ruff check custom_components tests
mypy --strict --explicit-package-bases -p custom_components.adaraz
```

The tests run the library's simulated reader (`adaraz.device.LinkDevice`)
against the integration's real WebSocket endpoint: pairing through the config
flow, punches, delivery after reconnects, relay commands and unpairing.

## Po polsku

Integracja Home Assistant dla urządzeń ADARAZ (pierwsze: czytnik kart /
rejestrator czasu pracy **RAZ-RCP-1**). Czytnik sam łączy się z Home Assistant
(WebSocket `/api/adaraz/ws`), a każde połączenie jest szyfrowane i uwierzytelniane
protokołem Noise, więc zwykłe `http://` w sieci lokalnej jest bezpieczne.

**Instalacja:** HACS (Custom repositories, typ Integration) albo skopiowanie
`custom_components/adaraz` do konfiguracji Home Assistant i restart.

**Połączenie czytnika:**

1. Ustaw adres Home Assistant w sieci lokalnej: **Ustawienia > System > Sieć**.
2. **Ustawienia > Urządzenia oraz usługi > Dodaj integrację > ADARAZ**.
3. Home Assistant pokaże adres serwera i jednorazowy, 12-znakowy kod
   w postaci `XXXX-XXXX-XXXX` (ważny 10 minut).
4. W lokalnym panelu czytnika: **Ustawienia > Integracje > Home Assistant
   i inne serwery** wpisz adres i kod.
5. Czytnik się połączy, a Home Assistant sam doda urządzenie.

**Encje:** zdarzenie *Odbicie* (atrybuty `uid`, `ts`, `late`), czujniki
*Połączenie* i *Czytnik kart* (problem), a gdy w czytniku włączono zdalne
sterowanie przekaźnikiem: przycisk *Otwórz* i przełącznik *Przekaźnik*.
Automatyzacje reagują na zmianę stanu encji *Odbicie* i sprawdzają atrybuty
`trigger.to_state` (przykład wyżej). Odbicia, które dotarły ponad 5 minut po
fakcie (czytnik był offline), mają `late: true` i nie powinny uruchamiać działań
w czasie rzeczywistym, np. otwierania drzwi.

**Tylko Home Assistant steruje przekaźnikiem:** w czytniku (firmware 108 lub
nowszy) wybierz tryb przekaźnika z *zarządzaniem zdalnym*. Karta wtedy tylko
zapisuje odbicie, a przekaźnik reaguje na *Otwórz* / *Przekaźnik* (i przyciski
testu w panelu). Bez Home Assistant albo sieci nikt nie wejdzie na kartę.

**Usunięcie:** usunięcie wpisu integracji odłącza połączony czytnik od Home
Assistant.

## License

MIT, see [LICENSE](LICENSE).
