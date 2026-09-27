# Library Upload - VMware Cloud Director 10.6 UI plug-in

Adds **Library Upload** to the Cloud Director top navigation (under **More**). It opens the organization's content library upload portal inside Cloud Director, so tenant administrators never leave their Cloud Director session or open another browser tab.

The plug-in only hosts the portal. The portal keeps its own sign-in with the tenant's local portal credentials (the administrator created by `deploy.sh tenant-add`), shown as a form inside the page. Credentials stay in memory until the page is closed or reloaded.

## How the address is chosen

| Signed-in user | Portal opened |
|---|---|
| Tenant organization `ACME-Bank` | `https://<library>/tenants/acme-bank/upload/` (organization name in lowercase) |
| Provider (System) | `https://<library>/upload/` (provider library) |

If an organization name cannot be a tenant name (3-32 lowercase letters, digits and hyphens), the plug-in says so instead of opening a wrong address. The rules live in `src/main/portal-url.ts` and are covered by `npm test`.

## Provider prerequisites

These four settings are the provider's responsibility, done once per environment or tenant:

1. **Tenant names match organizations.** Onboard each tenant with its Cloud Director organization name in lowercase, e.g. `deploy.sh tenant-add acme-bank` for organization `ACME-Bank`.
2. **Framing is allowed for Cloud Director.** In `/etc/vcsp/vcsp.conf` set `EMBED_ALLOWED_ORIGINS` to every Cloud Director address users browse to (the public URL and any aliases), then run `deploy.sh install`. Only these origins may show the portal in a frame; the library and API stay unframeable.
   ```bash
   EMBED_ALLOWED_ORIGINS="https://vcd.example.local"
   ```
3. **Browsers trust the library's certificate.** A browser cannot show a certificate warning inside a frame, so an untrusted certificate produces an empty frame. Install a certificate from a CA the users' browsers trust: `deploy.sh cert-csr`, then `deploy.sh cert-install`.
4. **Browsers can reach the library.** Users' workstations need DNS for the library's name and HTTPS (443) to it, just as for the standalone portal.

## Build

The library address is compiled into the plug-in. Build on any machine with Node.js 18 or 20 and internet access to registry.npmjs.org:

```bash
./package.sh --portal-url https://vcsp.example.local            # optional: --version 1.0.1
# -> library-upload-plugin-1.0.0.zip and .sha256
```

or reproducibly in Docker, on the same `node:18.20` base as Broadcom's plug-in template:

```bash
docker build --build-arg PORTAL_URL=https://vcsp.example.local --output type=local,dest=out .
```

`package.sh` writes the address into `src/main/portal-config.ts` and the manifest, runs the unit test, builds with `@vcd/plugin-builders` (Angular 17 and `manifestVersion` 4.0.0, as in Broadcom's Cloud Director Extension Standard Library template), and zips the result.

## Install in Cloud Director

1. Sign in to the provider portal and open **More > Customize Portal**.
2. **Upload** `library-upload-plugin-<version>.zip`.
3. Publish it to the tenant organizations that have a content library tenant. Optionally publish it to the provider scope too, where it opens the provider library.
4. Tenant administrators then find it under **More > Library Upload** after their next sign-in.

To change the library address, rebuild with the new `--portal-url` and upload the new version.

## Troubleshooting

- **The frame is empty or shows a browser error page.** Open the browser's developer console. `Refused to frame ... frame-ancestors` means the Cloud Director address is missing from `EMBED_ALLOWED_ORIGINS`. A certificate or name-resolution error means prerequisite 3 or 4 is not met. You can open the address shown above the frame in a new tab to see the error directly.
- **"The organization name ... cannot be used as a content library tenant name".** The organization's name does not fit the tenant naming rules; onboard it under a name that does, or rename the organization.
- **The portal asks to sign in again.** Credentials are deliberately kept only in memory. Reloading the page, or navigating away in Cloud Director and back, starts a new portal session.
- **Sign-in fails with a correct password.** Check the tenant in `deploy.sh tenant-show <name>`; the administrator must exist for that tenant, not for another one or the provider.

## Validate in your lab before production

This plug-in follows Broadcom's current template and documented extension points, but it has not run inside a real Cloud Director 10.6 instance yet. Confirm on your exact 10.6.x build that the upload is accepted (manifest version 4.0.0 with Angular 17), that the **More** menu entry appears for a published tenant, and that the frame height fits your layout.
