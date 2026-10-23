import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const APP_ID = 'io.github.jridder.ICloudLinux';
const UNIT = 'icloud.service';
const PHOTOS_UNIT = 'icloud-photos.service';
const STATE_DIR = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'state', 'icloud-linux']);
const POLL_SECONDS = 5;
const SYNC_FRAMES = 8;
const FRAME_MS = 150;

function run(argv) {
    return new Promise(resolve => {
        const proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
        proc.communicate_utf8_async(null, null, (p, res) => {
            try {
                const [, out] = p.communicate_utf8_finish(res);
                resolve({ok: p.get_successful(), out: out ?? ''});
            } catch (e) {
                resolve({ok: false, out: ''});
            }
        });
    });
}

function readStatus(name = 'status.json') {
    try {
        const [, bytes] = GLib.file_get_contents(GLib.build_filenamev([STATE_DIR, name]));
        return JSON.parse(new TextDecoder().decode(bytes));
    } catch (e) {
        return {};
    }
}

// Mirrors summarize() in gnome/icloud_gnome/window.py.
function summarize(unit, status) {
    const running = ['active', 'activating', 'reloading'].includes(unit.ActiveState);
    if (unit.LoadState !== 'loaded')
        return {icon: 'off', title: 'Not set up', setup: true};
    if (!running) {
        if (unit.ActiveState === 'failed')
            return {icon: 'error', title: 'Stopped unexpectedly'};
        return {icon: 'off', title: 'Off'};
    }
    if (status.pid !== Number(unit.MainPID))
        return {icon: 'syncing', title: 'Starting…'};
    const crawl = status.crawl ?? {};
    const hydration = status.hydration ?? {};
    if (status.phase === 'unauthenticated' || status.auth_error)
        return {icon: 'error', title: 'Sign-in required', auth: true};
    if (status.phase === 'starting' || status.phase === 'connecting')
        return {icon: 'syncing', title: 'Connecting…'};
    if (status.phase === 'scanning')
        return {icon: 'syncing', title: `Scanning… ${(crawl.entries ?? 0).toLocaleString()} items`, browsable: true};
    if (status.phase === 'reconciling')
        return {icon: 'syncing', title: 'Checking downloaded files…'};
    if (status.phase === 'stopped')
        return {icon: 'off', title: 'Stopping…'};
    if (status.uploading)
        return {icon: 'syncing', title: `Uploading ${status.pending_uploads ?? ''} changes…`, ready: true};
    if (status.refreshing)
        return {icon: 'syncing', title: 'Checking for changes…', ready: true};
    if (hydration.total && hydration.completed < hydration.total) {
        return {
            icon: 'syncing', ready: true,
            title: `Downloading ${hydration.completed.toLocaleString()} of ${hydration.total.toLocaleString()}`,
        };
    }
    return {icon: 'done', title: 'Up to date', ready: true};
}

const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'iCloud Drive');
        this._extension = extension;
        this._unit = {};
        this._photosUnit = {};
        this._status = {};

        this._icon = new St.Icon({style_class: 'system-status-icon'});
        this.add_child(this._icon);

        this._statusItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._statusItem.label.add_style_class_name('icloud-linux-status');
        this.menu.addMenuItem(this._statusItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._openItem = this.menu.addAction('Open iCloud Drive', () => this._openFolder(this._status, 'iCloud'));
        this._photosItem = this.menu.addAction('Open iCloud Photos', () => this._openFolder(readStatus('photos-status.json'), 'iCloud Photos'));
        this._syncItem = this.menu.addAction('Sync Now', () => {
            const units = this._photosUnit.ActiveState === 'active' ? [UNIT, PHOTOS_UNIT] : [UNIT];
            run(['systemctl', '--user', 'kill', '--kill-whom=main', '--signal=SIGUSR1', ...units]).then(() => this._poll());
        });
        this._authItem = this.menu.addAction('Sign In…', () => this._launchApp());

        this._switch = new PopupMenu.PopupSwitchMenuItem('iCloud Drive', false);
        this._switch.connect('toggled', (_item, state) =>
            run(['systemctl', '--user', state ? 'start' : 'stop', UNIT]).then(() => this._poll()));
        this.menu.addMenuItem(this._switch);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._settingsItem = this.menu.addAction('iCloud Drive Settings', () => this._launchApp());

        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._poll();
        });

        // Right-click: a short menu to turn everything off (or back on).
        this._contextMenu = new PopupMenu.PopupMenu(this, 0.0, St.Side.TOP);
        this._contextMenu.actor.add_style_class_name('panel-menu');
        this._contextMenu.actor.hide();
        Main.uiGroup.add_child(this._contextMenu.actor);
        Main.panel.menuManager.addMenu(this._contextMenu);
        this._powerItem = this._contextMenu.addAction('', () => this._togglePower());
        this._contextMenu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._contextMenu.addAction('iCloud Drive Settings', () => this._launchApp());

        GLib.mkdir_with_parents(STATE_DIR, 0o755);
        this._monitor = Gio.File.new_for_path(STATE_DIR).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
        this._monitor.connect('changed', (_m, file) => {
            if (file.get_basename() === 'status.json')
                this._update();
        });
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, POLL_SECONDS, () => {
            this._poll();
            return GLib.SOURCE_CONTINUE;
        });
        this._poll();
    }

    vfunc_event(event) {
        if (event.type() === Clutter.EventType.BUTTON_PRESS && event.get_button() === Clutter.BUTTON_SECONDARY) {
            this.menu.close();
            this._contextMenu.toggle();
            return Clutter.EVENT_STOP;
        }
        return super.vfunc_event(event);
    }

    _anyRunning() {
        return [this._unit, this._photosUnit].some(u => ['active', 'activating'].includes(u.ActiveState));
    }

    _togglePower() {
        // Off stops both services (and unmounts); they come back at the next
        // login, or right away from here.
        if (this._anyRunning()) {
            run(['systemctl', '--user', 'stop', UNIT, PHOTOS_UNIT]).then(() => this._poll());
            return;
        }
        const units = [UNIT];
        if (this._photosUnit.UnitFileState === 'enabled')
            units.push(PHOTOS_UNIT);
        run(['systemctl', '--user', 'start', ...units]).then(() => this._poll());
    }

    async _poll() {
        // `systemctl show` separates units with a blank line, in argument order.
        const {out} = await run(['systemctl', '--user', 'show', UNIT, PHOTOS_UNIT,
            '--property=LoadState,ActiveState,MainPID,UnitFileState']);
        const [unit, photosUnit] = out.split('\n\n').map(block => {
            const props = {};
            for (const line of block.split('\n')) {
                const i = line.indexOf('=');
                if (i > 0)
                    props[line.slice(0, i)] = line.slice(i + 1);
            }
            return props;
        });
        if (this._destroyed)
            return;
        this._unit = unit ?? {};
        this._photosUnit = photosUnit ?? {};
        this._update();
    }

    _update() {
        this._status = readStatus();
        const s = summarize(this._unit, this._status);
        this._setIcon(s.icon);
        this._statusItem.label.text = `iCloud Drive: ${s.title}`;
        this._openItem.visible = !s.setup;
        this._openItem.setSensitive(Boolean(s.ready || s.browsable));
        this._photosItem.visible = !s.setup && this._photosUnit.ActiveState === 'active';
        this._syncItem.visible = !s.setup && !s.auth;
        this._syncItem.setSensitive(Boolean(s.ready) && !this._status.refreshing && !this._status.uploading);
        this._authItem.visible = Boolean(s.auth);
        this._switch.visible = !s.setup;
        this._switch.setToggleState(['active', 'activating', 'reloading'].includes(this._unit.ActiveState));
        this._settingsItem.label.text = s.setup ? 'Set Up iCloud Drive…' : 'iCloud Drive Settings';
        this._powerItem.label.text = this._anyRunning() ? 'Turn Off iCloud' : 'Turn On iCloud';
        this._powerItem.visible = !s.setup;
    }

    _openFolder(status, fallback) {
        const mount = status.mountpoint || GLib.build_filenamev([GLib.get_home_dir(), fallback]);
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(mount).get_uri(), global.create_app_launch_context(0, -1));
    }

    _launchApp() {
        const app = Gio.DesktopAppInfo.new(`${APP_ID}.desktop`);
        if (app)
            app.launch([], global.create_app_launch_context(0, -1));
        else
            Main.notify('iCloud Drive', 'The iCloud Drive app is not installed.');
    }

    _iconFor(name) {
        return Gio.FileIcon.new(this._extension.dir.resolve_relative_path(`icons/${APP_ID}-${name}-symbolic.svg`));
    }

    _setIcon(name) {
        const animate = name === 'syncing' && St.Settings.get().enable_animations;
        if (name === this._iconName && animate === Boolean(this._frameTimer))
            return;
        this._iconName = name;
        this._stopAnimation();
        this._icon.gicon = this._iconFor(name);
        if (!animate)
            return;
        this._frames ??= [...Array(SYNC_FRAMES).keys()].map(i => this._iconFor(`syncing-${i}`));
        let frame = 0;
        this._frameTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FRAME_MS, () => {
            frame = (frame + 1) % SYNC_FRAMES;
            this._icon.gicon = this._frames[frame];
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopAnimation() {
        if (this._frameTimer) {
            GLib.source_remove(this._frameTimer);
            this._frameTimer = 0;
        }
    }

    destroy() {
        this._destroyed = true;
        this._stopAnimation();
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        this._monitor?.cancel();
        this._monitor = null;
        if (this._contextMenu) {
            Main.panel.menuManager.removeMenu(this._contextMenu);
            this._contextMenu.destroy();
            this._contextMenu = null;
        }
        super.destroy();
    }
});

export default class ICloudLinuxExtension extends Extension {
    enable() {
        this._indicator = new Indicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
