# Motif v0.3.1 – Clearer corrections and smoother format switching

- **Keep shared wording consistent.** Editing a Campaign headline or call to action updates every product in that language, with confirmation before replacing customized wording. Studio's **Apply to all products** button does the same and updates the matching Campaign field. It is a one-time action; later ordinary Studio edits remain specific to the selected product.
- **Find and correct prohibited wording.** Production, Studio, and Handoff identify the affected product and text. The correction opens the relevant field, and warnings update after the change saves. Handoff marks the affected image cards while leaving unrelated cards unmarked.
- **Keep editing when text runs outside an image.** Studio keeps the photograph, logo, and text visible so the producer can move or resize the text. The layout warning explains what needs adjusting, and download remains blocked until the problem is corrected.
- **Keep warnings readable.** Production warnings fit the sidebar, and Studio uses a compact notice above the editor. Available source photos remain distinct from wording or layout problems.
- **Keep the image visible when changing formats.** After an edit, Studio holds the displayed image until the selected format is ready, while leaving the format thumbnails in place.

Autosave, deliberate source-image generation, and creating finished images at download continue to work as in v0.3.0.

---

# Motif v0.3.0 – Autosave and simpler delivery

Campaign and Studio now save changes as you work, and Motif creates the finished campaign images when you download them.

- **Save as you work.** Campaign and Studio autosave into the same current editing instructions. Text, logo, color, and crop changes no longer require Apply or Update Images.
- **Create finished files at download.** Handoff previews the saved campaign. Download ZIP creates its finished PNGs once, checks them, and sends the package to the browser with progress messages.
- **Keep one current campaign.** Edits do not create campaign-version folders, retained finished PNG sets, or edit-history rows. Source imagery stays available for further editing.
- **Clearer image preparation.** Missing images have direct choices to select a photo or generate one. Production keeps progress and the next action together, including resuming after a key update. Uploaded scene guidance explains the background requirement.
- **Simpler local storage.** SQLite holds the current campaign and editing settings. Original imports, photos, logos, and provider source files stay in their campaign folder. New attachments use readable product or brand names with hyphens and unique identifiers. Working JSON mirrors and retained export archives are no longer created.

The supported upgrade preserves sources, editing choices, original imports, producer-added files, and existing exported packages. It retires verified obsolete working PNGs after the new current instructions are committed. Back up the complete Workspace with Motif stopped before upgrading. See [verification](verification.md) for the checked workflows and installation methods.

The following v0.2.0 notes are preserved as the historical release description.

# Motif v0.2.0 – Windows support and workflow refinements

This release brings Motif to Windows and makes the local workspace easier to navigate, with a single installation folder and refinements across campaign setup, editing, and handoff.

- **Added native Windows support.** Run Motif locally on Windows or macOS, with setup instructions for PowerShell and Terminal.
- **One installation folder.** The application, its Python environment, and the default Workspace live together inside **Motif**. Each campaign keeps its brief, images, and exports in its own named folder under **Workspace/Campaigns**.
- **More direct editing.** Drag images to reframe them in Studio. Use **Update Images** in Brand to apply campaign logo and default-color changes to existing images without another AI request. Colors customized in Studio remain under the producer’s control.
- **Clearer campaign setup and handoff.** Import briefs and attach images as you work, manage campaigns directly from the Workbench, and choose whether a campaign requires a logo. Field guidance and export notices make the next action clearer.
- **Updated documentation.** The included README and supporting guides describe installation, the campaign folder structure, and the current editing workflow.

To get started, extract the download, open the **Motif** folder, and follow the included **README.md**.
