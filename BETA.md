# Pre-releases

A pre-release is a finished version that a few users install some days before everyone else. It has the same version number, tag and ZIP file as the normal release it later becomes. HACS offers a pre-release only to those who turn on the `Pre-release` switch of this integration. Everyone else is not offered it as an update.

1. [Testing a pre-release](#testing-a-pre-release)
1. [Releasing a pre-release](#releasing-a-pre-release)

## Testing a pre-release

### Before you start

Make a Home Assistant backup in `Settings` > `System` > `Backups`. HACS makes none.

These pre-releases are for users of this fork of [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar). The way back from the fork to BJReplay/ha-solcast-solar is not tested.

To switch from BJReplay/ha-solcast-solar to this fork, keep this order. Both install to the folder `custom_components/solcast_solar`, and HACS names both `Solcast PV Forecast`.

1. In HACS, select ⋮ > `Custom repositories` and add `https://github.com/Bascht74/ha-solcast-solar` with type `Integration`.
1. Open the downloaded `Solcast PV Forecast`, which is BJReplay/ha-solcast-solar, and select ⋮ > `Remove`. This deletes the folder `custom_components/solcast_solar`. Do not restart Home Assistant before the fork is downloaded: until then the integration is missing, and HACS drops a custom repository that is not downloaded when it starts.
1. Open the fork in HACS with [this link](https://my.home-assistant.io/redirect/hacs_repository/?owner=Bascht74&repository=ha-solcast-solar&category=integration), select ⋮ > `Update information`, then `Download`.
1. Restart Home Assistant. Settings and forecasts stay, because they are stored outside that folder.

If both stay downloaded in HACS, the next update of BJReplay/ha-solcast-solar overwrites the fork.

### Turn on pre-releases

1. Go to `Settings` > `Devices & services` > `HACS`, open its devices and select `Solcast PV Forecast`.
1. Under `Diagnostic`, select `+1 entity not shown`, then `Pre-release`. In its settings (the gear), turn on `Enabled` and select `Update`. Home Assistant adds the entity after about 30 seconds.
1. Turn on the `Pre-release` switch. Its state is then `Pre-releases preferred`.
1. In HACS, open `Solcast PV Forecast` and select ⋮ > `Update information`. Otherwise HACS checks a custom repository only every 48 hours, and each restart of Home Assistant starts that time again.
1. `Settings` > `System` > `Updates` now offers the pre-release as `Solcast PV Forecast update`. Install it and restart Home Assistant.

With the switch on, HACS offers a pre-release only when it was released after the latest normal release.

To install one pre-release without the switch: in HACS, open `Solcast PV Forecast`, select ⋮ > `Redownload` > `Need a different version?`, choose the version marked as pre-release, select `Download` and restart Home Assistant.

### When the pre-release becomes the normal release

There is nothing to do. The installed version is the normal release now, so HACS shows no update. While the switch is on, HACS also offers the next pre-release. Turn the switch off to get normal releases only.

### Going back

These steps go back to the last normal release of this fork.

1. Turn off the `Pre-release` switch.
1. In HACS, open `Solcast PV Forecast`, select ⋮ > `Redownload` > `Need a different version?`, choose the last normal release, select `Download` and restart Home Assistant. HACS does not offer an older version as an update by itself.
1. If the notes of the pre-release say that it changes stored data, restore the backup instead.

The action `update.install` does the same as step 2, with the update entity of the HACS device `Solcast PV Forecast` as `entity_id` (its device page shows it) and `version` set to the tag, for example `v4.7.0.15`.

### Feedback

Open an issue with the template `Pre-release feedback`. "It works" helps as much as a problem report.

## Releasing a pre-release

1. Raise `version` in `manifest.json` and add `.github/release-notes/<version>.md`, as for every release.
1. Add `.github/release-notes/<version>.pre-release.md`: what to test, until when, whether stored data changes, and the way back.
1. Merge to `main`. When the tests pass, `auto-release.yml` creates the release `v<version>` as a pre-release, with the pre-release text before the notes.
1. When the test time is over, run `Actions` > `Promote the pre-release` > `Run workflow` on `main`. The release keeps its tag and ZIP, loses the pre-release mark and the pre-release text, and becomes the latest release.

While a pre-release is open, every new version from `main` goes out as a pre-release too, or the open one is promoted before the version is raised. A normal release would bring the untested changes to everyone, so `auto-release.yml` stops with an error instead. Adding `.github/release-notes/<version>.pre-release.md` for the new version to `main` then releases it as a pre-release.

When a pre-release has a problem, fix it and release the next version number as a pre-release again. The first one stays a pre-release, and HACS skips it.

A pre-release that raises `CONFIG_VERSION` or `JSON_VERSION` says so in its pre-release text. After going back, Home Assistant refuses a config entry with a higher version, and the older version uses a cache file with a higher version as it is. Going back then needs the backup.

Removing the pre-release mark by hand also works: edit the release, clear `Set as a pre-release`, select `Set as the latest release` and remove the pre-release text. `release.yml` then builds the ZIP again from the same tag.
