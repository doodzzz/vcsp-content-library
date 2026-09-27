import { Component, Inject } from "@angular/core";
import { DomSanitizer, SafeResourceUrl } from "@angular/platform-browser";
import { SESSION_ORGANIZATION, SESSION_SCOPE } from "@vcd/sdk";
import { PORTAL_BASE_URL } from "./portal-config";
import { portalUrlFor } from "./portal-url";

/**
 * Shows the organization's upload portal inside Cloud Director. The portal keeps its own
 * sign-in (the tenant's local portal credentials); this component only hosts it.
 */
@Component({
    selector: "vcsp-library-upload",
    template: `
        <div class="lu-page">
            <div class="lu-bar">
                <h2 class="lu-title">Library Upload</h2>
                <span class="lu-url" *ngIf="url">{{ url }}</span>
                <span class="lu-spacer"></span>
                <button *ngIf="url" type="button" class="btn btn-sm btn-link" (click)="reload()">Reload</button>
            </div>
            <div *ngIf="problem" class="alert alert-danger" role="alert">
                <div class="alert-items">
                    <div class="alert-item static">
                        <span class="alert-text">{{ problem }}</span>
                    </div>
                </div>
            </div>
            <iframe *ngIf="frameUrl" class="lu-frame" [src]="frameUrl" title="Content library upload portal"
                    allow="clipboard-write" referrerpolicy="no-referrer"></iframe>
        </div>
    `,
    styles: [`
        .lu-page { display: flex; flex-direction: column; height: calc(100vh - 7.5rem); min-height: 36rem; padding: 0.5rem 1rem 0; }
        .lu-bar { display: flex; align-items: baseline; gap: 1rem; padding-bottom: 0.5rem; }
        .lu-title { margin: 0; }
        .lu-url { font-family: monospace; font-size: 0.75rem; opacity: 0.75; overflow-wrap: anywhere; }
        .lu-spacer { flex: 1; }
        .lu-frame { flex: 1; width: 100%; border: 1px solid rgba(128, 128, 128, 0.35); border-radius: 3px; background: #fff; }
    `],
})
export class LibraryUploadComponent {
    url = "";
    problem = "";
    frameUrl: SafeResourceUrl | null = null;

    constructor(
        @Inject(SESSION_SCOPE) scope: string,
        @Inject(SESSION_ORGANIZATION) organization: string,
        private sanitizer: DomSanitizer,
    ) {
        const target = portalUrlFor(PORTAL_BASE_URL, scope, organization);
        if (target.error) {
            this.problem = target.error;
        } else {
            this.url = target.url;
            this.frameUrl = this.sanitizer.bypassSecurityTrustResourceUrl(this.url);
        }
    }

    reload(): void {
        this.frameUrl = null;
        setTimeout(() => { this.frameUrl = this.sanitizer.bypassSecurityTrustResourceUrl(this.url); });
    }
}
