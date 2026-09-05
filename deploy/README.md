# Metrics publication without a VPN startup dependency

Production publishes WGBot metrics on IPv4 `0.0.0.0:9200`. This allows Docker to
start the container before Cascade creates the VPN address. Monitoring continues
to use `http://10.8.2.1:9200/metrics`.

The host firewall permits TCP/9200 only from loopback and from `10.8.2.0/24` arriving
on `wg12`. A source address alone is not sufficient. Do not publish this port on
all interfaces before installing the firewall restrictions.

## Firewall and persistence

`wgbot-metrics.rules.v4` is a one-time additive installation fragment for
`iptables-restore --noflush`; Docker's `DOCKER-USER` chain must already exist.
Check that `WGBOT_METRICS` does not already exist before installing it. Reapplying
the fragment would duplicate rules.

The INPUT jump protects host listeners, including Docker proxy. The DOCKER-USER
jump matches original-direction DNAT connections using the original destination
port, because Docker has already translated the destination at that point. The
shared chain returns permitted traffic to existing host/Docker rules and drops
all other matching traffic. Other ports and chain policies remain unchanged.

Persist only these additions in the existing `/etc/iptables/rules.v4`, retaining
all unrelated lines. Declare the new chain within `*filter`, before its rules.
Convert the fragment's `-I INPUT 1` and `-I DOCKER-USER 1` commands to `-A INPUT`
and `-A DOCKER-USER`, respectively, placing them before existing rules for those
chains. Validate the complete candidate with `iptables-restore --test` before
atomically replacing the saved file. Existing `netfilter-persistent` restores it
at boot; do not reload the entire live firewall or overwrite the saved rules with
a full live dump. No additional service is required.

Production keeps IPv4 publication explicit. IPv6 publication and Docker direct
routing are not enabled by this change. Review the filtering if either is enabled
later. The Compose default stays on loopback for other installations.

## Runtime operations

Back up `.env`, Compose configuration, current/saved firewall rules and SQLite
before changing the production binding. Change only `METRICS_BIND_ADDRESS` to
`0.0.0.0`; retain port `9200`, the image and mounts. Recreate only WGBot after the
firewall is installed. The application and its Cascade validation are unchanged.

Deployment and rollback hold `.runtime.lock` from before runtime configuration
changes through health validation and image persistence. Manual Compose operations
should hold the same lock. Do not delete the lock file while it is in use.

## Validation

On a Linux test host with Python, iproute2, iptables and curl:

```sh
sudo python3 deploy/test_metrics_firewall.py
```

This creates disposable namespaces and tests loopback, VPN, WAN, a spoofed VPN
source on WAN, and DNAT to a different backend port. It also checks wildcard
listener startup before the VPN address exists. It does not restart Docker or
change the host firewall.

On production, check metrics from an actual VPN client and confirm the public IP
on TCP/9200 times out from an external client. Check `/health`, public webhook
health, Cascade validation, polling, image/mounts, SQLite integrity and ten minutes
of stable Docker health with no restarts. Do not restart production Docker for a
startup-order test.

## Rollback

Restore the VPN binding and recreate WGBot only after `10.8.2.1` exists. Then remove
the metrics-specific firewall jumps/chain and their saved-file additions if needed.
Do not remove filtering while the wildcard publication remains active. Keep the
backups and runtime data. The previous recovery service and script are no longer
needed and should remain uninstalled during normal operation.
