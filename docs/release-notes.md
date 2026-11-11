# Unreleased – Portfolio maintenance

This local candidate retains Motif's Python and plain JavaScript stack. It has not been published as a release.

- Canceling a rename or prohibited-word dialog leaves the applied campaign values unchanged.
- Campaign copy confirmation waits until the field loses focus or the producer explicitly saves or navigates, preserving custom wording until confirmation.
- Studio remains usable after resetting text placement, opening source details while a description saves, or styling an empty call to action.
- Clearing a headline keeps the product visible and editable. Export remains blocked until required wording is restored.
- Preview planning and gallery images use bounded, content-checked caches to avoid repeating image decoding and layout work. Temporary editor PNGs use faster lossless compression.
- Image generation honors its configured overall response deadline without an earlier 60-second read-inactivity cutoff. Automatic paid retries remain disabled, and uncertain outcomes remain recorded.

See the [verification notes](verification.md) for the candidate's checks and measured performance.

---

# Motif v0.3.2 – Reliable installation and Studio editing

- Fixed a white-canvas flicker when attaching the first campaign logo in Studio and then moving or resizing it. The photograph and wording stay visible while the logo is arranged.
- Fixed a macOS startup error that could prevent a fresh installation from finding the `campaign_studio` package.
- After starting the server, you can click the workbench or health-check address in the README on GitHub. Both links open on the computer running Motif.
- Inside a Studio product, you can open the campaign menu and dismiss it with Escape or the close button, then keep editing. You can also switch campaigns or return to the workbench. Motif saves pending edits before opening the menu.

---

# Motif v0.3.1 – Consistent wording, clearer corrections, and retained assets

This update keeps Campaign and Studio wording in sync, makes corrections easier to follow, and preserves uploaded photos and logos when they are removed from a campaign.

- **Keep shared wording consistent.** Editing a Campaign headline or call to action updates every product in that language, with confirmation before replacing customized wording. Studio’s **Apply to all products** button updates only the selected text field and the matching Campaign field. It is a one-time action; later Studio typing again changes only that product’s wording across its three formats.
- **Show the current wording while correcting it.** Campaign previews and Studio display the saved headline and call to action even when they contain prohibited words. Removing one of several prohibited words updates the preview and leaves warnings only for the words that remain. Export stays blocked until prohibited wording is resolved.
- **Restore unfinished wording from an earlier build.** Reopening a campaign brings those fields into the current save flow, with confirmation before replacing customized product wording.
- **Show which text needs attention.** One consolidated warning lets the producer move through affected products and fields. Each correction opens its Campaign or Studio text field, and Handoff also marks the affected image cards. Warnings disappear as the corrections save.
- **Keep editing when text runs outside an image.** Studio keeps the photograph, logo, and text visible so the producer can move or resize the text. Download remains blocked until the layout is corrected.
- **Keep source preparation focused.** Production handles existing and generated product photos. Wording and logo choices do not block source preparation or access to Studio.
- **Make logos optional and easy to attach.** Images can be edited and exported without a logo. Handoff reminds the producer when none is attached. The Studio Logo tool immediately offers attachment, and both the canvas and sidebar upload buttons work for first attachment and replacement.
- **Keep removed assets available.** Removing a product or logo, or replacing an attachment, keeps the uploaded files in the campaign’s Assets folder for reuse. Deleting the campaign still removes its registered files, including retained attachments.
- **Keep images visible when switching formats.** Studio holds the displayed image until its replacement is ready and keeps the three format thumbnails visible.
- **Open new campaigns without configured rules.** A campaign created from scratch opens and remains editable before prohibited words are added.
- **Simplify page headings.** The four main pages keep their titles without repeating the numbered navigation labels.

Autosave, deliberate AI source-image generation, and creation of finished PNGs during download continue as in v0.3.0. See the [verification record](verification.md) for the checks performed and their scope.

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
