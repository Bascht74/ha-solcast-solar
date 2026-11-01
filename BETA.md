# Pre-releases

A pre-release is a finished version that a few users install some days before everyone else. It has the same version number, tag and ZIP file as the normal release it later becomes. The Marketplace of Home Assistant offers a pre-release only to those who turn on the `Pre-release` switch of this integration. Everyone else is not offered it as an update. HACS on an older Home Assistant has the same switch.

1. [Testing a pre-release](#testing-a-pre-release)
1. [Releasing a pre-release](#releasing-a-pre-release)

## Testing a pre-release

### Before you start

Make a Home Assistant backup in `Settings` > `System` > `Backups`. The Marketplace makes none.

These pre-releases are for users of this fork of [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar). The way back from the fork to BJReplay/ha-solcast-solar is not tested.

To switch from BJReplay/ha-solcast-solar to this fork, keep this order: add `https://github.com/Bascht74/ha-solcast-solar` as a custom repository of type `Integration`, remove the downloaded BJReplay/ha-solcast-solar, download the fork and restart Home Assistant. Both install to the folder `custom_components/solcast_solar`, so if both stay downloaded, the next update of BJReplay/ha-solcast-solar overwrites the fork. Settings and forecasts stay, because they are stored outside that folder.

### Turn on pre-releases

1. Go to `Settings` > `Devices & services` > `Marketplace`, open its devices and select `Solcast PV Forecast`.
1. Select the entity `Pre-release`, which is not shown at first. In its settings (the gear), turn on `Enabled` and select `Update`. Home Assistant adds the entity after about 30 seconds.
1. Turn on the `Pre-release` switch.
1. `Settings` > `System` > `Updates` offers the pre-release as `Solcast PV Forecast update` once the Marketplace has checked the repository. Install it and restart Home Assistant.

To install a pre-release at once, with or without the switch, run the action `update.install` with the update entity of `Solcast PV Forecast` as `entity_id` and `version` set to the tag, for example `v4.7.0.16`. Then restart Home Assistant.

### When the pre-release becomes the normal release

There is nothing to do. The installed version is the normal release now, so no update is shown. While the switch is on, the next pre-release is offered too. Turn the switch off to get normal releases only.

### Going back

These steps go back to the last normal release of this fork.

1. Turn off the `Pre-release` switch.
1. Run the action `update.install` with the update entity of `Solcast PV Forecast` as `entity_id` and `version` set to the last normal release, for example `v4.7.0.15`, and restart Home Assistant. An older version is not offered as an update by itself.
1. If the notes of the pre-release say that it changes stored data, restore the backup instead.

### Feedback

Open an issue with the template `Pre-release feedback`. "It works" helps as much as a problem report.

## Releasing a pre-release

1. Raise `version` in `manifest.json` and add `.github/release-notes/<version>.md`, as for every release.
1. Add `.github/release-notes/<version>.pre-release.md`: what to test, until when, whether stored data changes, and the way back.
1. Merge to `main`. When the tests pass, `auto-release.yml` creates the release `v<version>` as a pre-release, with the pre-release text before the notes.
1. When the test time is over, run `Actions` > `Promote the pre-release` > `Run workflow` on `main`. The release keeps its tag and ZIP, loses the pre-release mark and the pre-release text, and becomes the latest release.

While a pre-release is open, every new version from `main` goes out as a pre-release too, or the open one is promoted before the version is raised. A normal release would bring the untested changes to everyone, so `auto-release.yml` stops with an error instead. Adding `.github/release-notes/<version>.pre-release.md` for the new version to `main` then releases it as a pre-release.

When a pre-release has a problem, fix it and release the next version number as a pre-release again. The first one stays a pre-release, and the Marketplace skips it.

A pre-release that raises `CONFIG_VERSION` or `JSON_VERSION` says so in its pre-release text. After going back, Home Assistant refuses a config entry with a higher version, and the older version uses a cache file with a higher version as it is. Going back then needs the backup.

Removing the pre-release mark by hand also works: edit the release, clear `Set as a pre-release`, select `Set as the latest release` and remove the pre-release text. `release.yml` then builds the ZIP again from the same tag.
