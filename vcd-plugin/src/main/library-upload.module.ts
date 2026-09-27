import { CommonModule } from "@angular/common";
import { NgModule } from "@angular/core";
import { RouterModule, Routes } from "@angular/router";
import { VcdSdkModule } from "@vcd/sdk";
import { LibraryUploadComponent } from "./library-upload.component";

export { LibraryUploadComponent } from "./library-upload.component";

const ROUTES: Routes = [{ path: "", component: LibraryUploadComponent }];

@NgModule({
    imports: [CommonModule, RouterModule.forChild(ROUTES), VcdSdkModule.forRoot()],
    declarations: [LibraryUploadComponent],
    exports: [LibraryUploadComponent],
})
export class LibraryUploadModule {}
