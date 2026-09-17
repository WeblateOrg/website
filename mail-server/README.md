# Mail server contact page

Check out this repository at `/srv/website` on each mail delivery node. The
included `nginx.conf` serves `/srv/website/mail-server/index.html` for
`md.weblate.org`. This is a standalone English page with no build step, Django,
or JavaScript. It uses the website's
layout classes and loads CSS, fonts, and branding assets from
`https://weblate.org/static/`. These assets must remain accessible for the page
to display with the website's styling; the contact text is in the HTML itself.

Serve the page over HTTPS with a valid certificate and redirect HTTP to HTTPS.
Ensure all IPv4 and IPv6 addresses published for the hostname serve the page.

The page requests `noindex` to discourage search indexing. Keep it publicly
accessible so mail operators can find the contact directly from the PTR hostname.

## Central certificate issuance and SSH deployment

These scripts assume Debian/Ubuntu, nginx managed by systemd, Bash, GNU coreutils,
GNU tar, OpenSSL, and OpenSSH. Certbot runs as an ordinary user on a trusted node.
The three web servers receive the same certificate and private key over SSH;
Cloudflare credentials remain on the trusted node. This configures HTTPS only,
not SMTP TLS.

### Trusted node

Install dependencies as an administrator:

```sh
sudo apt install certbot python3-certbot-dns-cloudflare openssh-client util-linux
```

As the ordinary user that will run the renewal job:

```sh
install -d -m 700 ~/.config/certbot ~/.ssh
install -m 600 /dev/null ~/.config/certbot/cloudflare.ini
editor ~/.config/certbot/cloudflare.ini
ssh-keygen -t ed25519 -f ~/.ssh/mail-certificate -N ''
```

Store the token in `cloudflare.ini` (do not commit it):

```ini
dns_cloudflare_api_token = YOUR_TOKEN
```

Use a Cloudflare API token with `Zone / DNS / Edit` restricted to `weblate.org`.
Optionally restrict its client IPs to the trusted node's outbound addresses.
The token still permits all DNS edits in that zone.

Create three unique SSH aliases in `~/.ssh/config`, pointing at each node's
individual address, not the shared `md.weblate.org` hostname. For example:

```sshconfig
Host mail-cert-1
    HostName 49.13.15.187
Host mail-cert-2
    HostName 157.180.32.68
Host mail-cert-3
    HostName 23.88.100.31
Host mail-cert-1 mail-cert-2 mail-cert-3
    User certdeploy
    IdentityFile ~/.ssh/mail-certificate
    IdentitiesOnly yes
```

Confirm these addresses still match your nodes. Add their SSH host keys to
`~/.ssh/known_hosts` after verifying fingerprints through your existing trusted
administration access. The script requires known host keys and never accepts new
ones automatically.

### Each mail node (administrator setup)

Install nginx, OpenSSL, CA certificates, sudo, and util-linux. Create a dedicated
`certdeploy` SSH account with a home directory and a shell, but no general sudo
access. Use the helper directly from the checkout:

```sh
sudo chown root:root /srv/website/mail-server/install-mail-certificate.sh
sudo chmod 755 /srv/website/mail-server/install-mail-certificate.sh
sudo visudo -f /etc/sudoers.d/mail-certificate
```

Add this rule; the empty quoted argument list permits no arguments:

```sudoers
certdeploy ALL=(root) NOPASSWD: /srv/website/mail-server/install-mail-certificate.sh ""
```

Put the trusted node's public key in `~certdeploy/.ssh/authorized_keys` with this
prefix (replace the example key with the generated public key):

```text
restrict,command="sudo -n /srv/website/mail-server/install-mail-certificate.sh" ssh-ed25519 AAAA... mail-certificate
```

Set `.ssh` to mode 700 and `authorized_keys` to 600, owned by `certdeploy`.
This key only runs the installer; it cannot open a shell or forward ports.
Keep the helper, its parent directories (including the checkout), and
`/etc/ssl/md.weblate.org` root-owned and non-writable by `certdeploy` or other
unprivileged users: the helper is executed directly from the checkout as root.

Install and enable the matching nginx site configuration:

```sh
sudo install -o root -g root -m 644 /srv/website/mail-server/nginx.conf \
    /etc/nginx/sites-available/md.weblate.org
sudo ln -s /etc/nginx/sites-available/md.weblate.org \
    /etc/nginx/sites-enabled/md.weblate.org
```

Before enabling the site for the first time, ensure nginx is already running
with its existing configuration. Do not restart or reload it after enabling the
new site until the certificates exist: the first successful certificate deployment
creates them, tests the configuration, and reloads nginx. On subsequent site
configuration updates, run `sudo nginx -t && sudo systemctl reload nginx`.

The site serves only `/` and `/index.html`, so the deployment scripts, README,
and nginx configuration alongside the page are not exposed. Make the checkout's
parent directories traversable and `index.html` readable by nginx's worker user.
Allow ports 80/443 on IPv4 and IPv6. Certificates stay outside the checkout at
`/etc/ssl/md.weblate.org/current/`, matching the installer's paths.

Helper updates in the checkout take effect on the next deployment; preserve its
ownership and permissions. After updating nginx configuration in the checkout,
repeat its `install` command to update the root-owned copy. The certificate
deployment SSH key does not have permission to update the checkout or nginx
configuration.

### Test, issue, and schedule

On the trusted node, as the ordinary user, from this directory:

```sh
# Test DNS validation against Let's Encrypt staging; deploy nothing.
./renew-certificates.sh test mail-cert-1 mail-cert-2 mail-cert-3

# Obtain or renew the production certificate, then deploy to every node.
./renew-certificates.sh run mail-cert-1 mail-cert-2 mail-cert-3

# Retry deployment without contacting Let's Encrypt.
./renew-certificates.sh deploy mail-cert-1 mail-cert-2 mail-cert-3
```

Certbot state is stored under `~/.local/share/mail-server-certbot/`, including
its account key, certificate private key, working files, and logs. Override with
`CERTBOT_STATE_DIR` if needed. `CLOUDFLARE_CREDENTIALS` overrides the credentials
path and `CERTBOT_EMAIL` overrides the default `michal@cihar.com` account email.

Schedule the `run` command twice daily in this user's crontab, replacing the
script path with its absolute installed location:

```cron
17 3,15 * * * /absolute/path/renew-certificates.sh run mail-cert-1 mail-cert-2 mail-cert-3
```

Ensure cron's `PATH` includes the installed tools and monitor nonzero exits and
certificate expiry. No root Certbot timer is needed for this user-owned state.

Every run attempts every node, even when renewal is not due; unchanged files do
not trigger a reload. A failed node is retried on the next run. Each installer
checks certificate trust, validity, hostname, and the matching private key before
atomically switching a symlink to the new pair. nginx test/reload failures restore
the previous pair. Old successful releases remain in `releases/` for recovery;
prune older releases periodically while retaining the current and previous ones.

References: [Certbot user guide](https://eff-certbot.readthedocs.io/en/stable/using.html)
and [Cloudflare plugin](https://certbot-dns-cloudflare.readthedocs.io/en/stable/).
