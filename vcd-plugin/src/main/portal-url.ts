/**
 * Works out which upload portal to open for the signed-in Cloud Director user.
 *
 * Convention (a provider prerequisite when onboarding): the content library tenant
 * name is the Cloud Director organization name in lowercase, created with
 * `deploy.sh tenant-add <name>`. Provider (System) users get the provider portal.
 */
export const TENANT_NAME = /^[a-z][a-z0-9-]{1,30}[a-z0-9]$/;
const BASE_URL = /^https:\/\/[A-Za-z0-9.-]+(:\d{1,5})?$/;

export interface PortalTarget {
    url?: string;
    error?: string;
}

export function portalUrlFor(baseUrl: string, scope: string, organization: string): PortalTarget {
    const base = (baseUrl || "").trim().replace(/\/+$/, "");
    if (!BASE_URL.test(base) || base === "https://vcsp.example.local") {
        return { error: "This plug-in was packaged without the content library address. Ask your provider to rebuild it with: package.sh --portal-url https://<library-address>" };
    }
    if (scope === "service-provider") {
        return { url: `${base}/upload/` };
    }
    const tenant = (organization || "").trim().toLowerCase();
    if (!TENANT_NAME.test(tenant)) {
        return { error: `The organization name "${organization}" cannot be used as a content library tenant name (3-32 lowercase letters, digits and hyphens). Ask your provider to onboard a tenant for this organization.` };
    }
    return { url: `${base}/tenants/${tenant}/upload/` };
}
