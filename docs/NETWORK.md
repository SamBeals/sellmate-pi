# SellMate preferred uplink (router / LTE gateway over Wi-Fi)

SellMate kiosks often have two paths to the internet:

1. **Preferred uplink** — Ethernet from the Pi to a travel router with LTE
   (for example a GL.iNet Spitz). The Pi sees a normal Ethernet / NM
   `802-3-ethernet` profile, not a cellular modem.
2. **Wi-Fi fallback** — site Wi-Fi for when the router link is down.

Provisioning configures **NetworkManager connection profiles** so the preferred
uplink has a **lower `ipv4.route-metric`** than Wi-Fi. Both stay
`connection.autoconnect=yes`. Failover and failback are handled by NetworkManager;
no custom daemon and no modem-vendor hard-coding.

## How it works

| Role | How profiles are chosen | Default metric |
|---|---|---|
| Preferred | `SELLMATE_PREFERRED_CONNECTIONS` or all `802-3-ethernet` | `100` |
| Wi-Fi | `SELLMATE_WIFI_CONNECTIONS` or all `802-11-wireless` | `700` |

Lower metric wins the default route.

- Preferred up → traffic uses preferred.
- Preferred down → Wi-Fi becomes default automatically.
- Preferred returns → preferred wins again automatically.

Settings are persisted on the NM profile (`nmcli connection modify`), so they
survive reboot.

## Install (one-time on each Pi)

```bash
cd ~/sellmate-pi   # or your clone path
git pull

# Optional: discover exact connection names first
nmcli -t -f NAME,TYPE connection show

sudo ./provisioning/network/install-preferred-uplink.sh

# Or pin names explicitly (spaces OK):
# sudo ./provisioning/network/install-preferred-uplink.sh \
#   --preferred "Wired connection 1" \
#   --wifi "StoreGuest" \
#   --preferred-metric 100 \
#   --wifi-metric 700

./provisioning/network/verify-preferred-uplink.sh
```

Dry-run (no changes):

```bash
sudo ./provisioning/network/install-preferred-uplink.sh --dry-run
```

Config is written to `/etc/sellmate/network.env` (shell-quoted). Example:
[`provisioning/network/network.env.example`](../provisioning/network/network.env.example).

## Configuring or renaming the preferred connection

1. List profiles: `nmcli connection show`
2. Set names in `/etc/sellmate/network.env`:

```bash
SELLMATE_PREFERRED_CONNECTIONS='Wired connection 1'
SELLMATE_WIFI_CONNECTIONS='MyShopWiFi'
SELLMATE_PREFERRED_METRIC='100'
SELLMATE_WIFI_METRIC='700'
```

3. Re-run the installer (it sources the env file):

```bash
sudo ./provisioning/network/install-preferred-uplink.sh
```

Empty preferred/wifi lists mean “all ethernet” / “all wifi” profiles. To prefer a
non-ethernet profile later (for example a named `gsm` profile), put that exact NM
connection name in `SELLMATE_PREFERRED_CONNECTIONS`.

## Verification

Use the script:

```bash
./provisioning/network/verify-preferred-uplink.sh
```

Or inspect manually:

```bash
# Active connections
nmcli -f NAME,TYPE,DEVICE,STATE connection show --active

# Metrics on a profile
nmcli -g ipv4.route-metric,connection.autoconnect connection show "Wired connection 1"

# Current default route and device
ip -4 route show default
```

### Failover test

1. With preferred + Wi-Fi both configured, run verify — default device should be
   the Ethernet/router interface.
2. Unplug the preferred uplink Ethernet.
3. Wait a few seconds; run verify again — default route should move to Wi-Fi
   (`wlan0` or similar) with the higher metric.
4. Replug preferred uplink — default should return to the preferred device.

## Relationship to SellMate services

`vend-api`, `sellmate-poller`, `sellmate-health`, and the touchscreen unit already
wait on `network-online.target`. They do not choose uplinks; once NetworkManager
has a default route, services behave the same on either path.
