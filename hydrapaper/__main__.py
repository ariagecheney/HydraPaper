# __main__.py
#
# Copyright (C) 2017 GabMus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys
import os
import pathlib
import json

import argparse
from gi.repository import Gtk, Wnck, Gdk, Gio, GdkPixbuf

from . import monitor_parser as MonitorParser
from . import wallpaper_merger as WallpaperMerger
from . import threading_helper as ThreadingHelper
from . import listbox_helper as ListboxHelper
from . import wallpaper_flowbox_item as WallpaperFlowboxItem
from . import wallpapers_folder_listbox_row as WallpapersFolderListBoxRow

import hashlib # for pseudo-random wallpaper name generation


HOME = os.environ.get('HOME')
G_CONFIG_FILE_PATH = '{0}/.config/hydrapaper.json'.format(HOME)
HYDRAPAPER_CACHE_PATH = '{0}/.cache/hydrapaper'.format(HOME)

# check if inside flatpak sandbox. if so change some variables
if 'XDG_RUNTIME_DIR' in os.environ.keys():
    if os.path.isfile('{0}/flatpak-info'.format(os.environ['XDG_RUNTIME_DIR'])):
        G_CONFIG_FILE_PATH = '{0}/hydrapaper.json'.format(os.environ.get('XDG_CONFIG_HOME'))
        HYDRAPAPER_CACHE_PATH = '{0}/hydrapaper'.format(os.environ.get('XDG_CACHE_HOME'))

IMAGE_EXTENSIONS = [
    '.jpg',
    '.jpeg',
    '.png',
    '.tiff',
    '.svg'
]


class Application(Gtk.Application):
    def __init__(self, **kwargs):
        self.builder = Gtk.Builder.new_from_resource(
            '/org/gabmus/hydrapaper/ui/ui.glade'
        )
        super().__init__(
            application_id='org.gabmus.hydrapaper',
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
            **kwargs
        )
        self.RESOURCE_PATH = '/org/gabmus/hydrapaper/'

        self.CONFIG_FILE_PATH = G_CONFIG_FILE_PATH  # G stands for Global (variable)

        self.configuration = self.get_config_file()

        self.builder.connect_signals(self)

        settings = Gtk.Settings.get_default()
        # settings.set_property("gtk-application-prefer-dark-theme", True)

        self.window = self.builder.get_object('window')

        self.window.set_icon_name('org.gabmus.hydrapaper')

        self.window.resize(
            self.configuration['windowsize']['width'],
            self.configuration['windowsize']['height']
        )

        self.builder.get_object('wallpapersFoldersActionbar').pack_start(
            self.builder.get_object('wallpapersFoldersActionbarButtonbox')
        )

        # Lock when refreshing wallpapers to avoid duplicates generated by spam presses
        self.wallpapers_refreshing_locked = False

        self.mainBox = self.builder.get_object('mainBox')
        self.apply_button = self.builder.get_object('applyButton')
        self.apply_spinner = self.builder.get_object('applySpinner')

        self.monitors_flowbox = self.builder.get_object('monitorsFlowbox')
        self.wallpapers_flowbox = self.builder.get_object('wallpapersFlowbox')
        self.wallpapers_flowbox_favorites = self.builder.get_object('wallpapersFlowboxFavorites')

        self.keep_favorites_in_mainview_toggle = self.builder.get_object('keepFavoritesInMainviewToggle')

        self.keep_favorites_in_mainview_toggle.set_active(
            self.configuration['favorites_in_mainview']
        )

        self.wallpaper_selection_mode_toggle = self.builder.get_object('wallpaperSelectionModeToggle')

        self.wallpaper_selection_mode_toggle.set_active(
            not self.configuration['selection_mode'] == 'single'
        )

        self.add_to_favorites_toggle = self.builder.get_object('addToFavoritesButton')
        self.favorites_button_clicked = False

        self.wallpapers_flowbox_favorites.set_activate_on_single_click(
            self.configuration['selection_mode'] == 'single'
        )
        self.wallpapers_flowbox.set_activate_on_single_click(
            self.configuration['selection_mode'] == 'single'
        )

        self.selected_wallpaper_path_entry = self.builder.get_object('selectedWallpaperPathEntry')

        self.wallpapers_flowbox_itemoptions_popover = self.builder.get_object('wallpapersFlowboxItemoptionsPopover')

        # handle longpress gesture for wallpapers_flowbox
        self.wallpapers_flowbox_longpress_gesture = Gtk.GestureLongPress.new(self.wallpapers_flowbox)
        self.wallpapers_flowbox_longpress_gesture.set_propagation_phase(Gtk.PropagationPhase.TARGET)
        self.wallpapers_flowbox_longpress_gesture.set_touch_only(False)
        self.wallpapers_flowbox_longpress_gesture.connect("pressed", self.on_wallpapersFlowbox_rightclick_or_longpress, self.wallpapers_flowbox)

        self.wallpapers_flowbox_favorites_longpress_gesture = Gtk.GestureLongPress.new(self.wallpapers_flowbox_favorites)
        self.wallpapers_flowbox_favorites_longpress_gesture.set_propagation_phase(Gtk.PropagationPhase.TARGET)
        self.wallpapers_flowbox_favorites_longpress_gesture.set_touch_only(False)
        self.wallpapers_flowbox_favorites_longpress_gesture.connect("pressed", self.on_wallpapersFlowbox_rightclick_or_longpress, self.wallpapers_flowbox_favorites)

        self.errorDialog = Gtk.MessageDialog()
        self.errorDialog.add_button('Ok', 0)
        self.errorDialog.set_default_response(0)
        self.errorDialog.set_transient_for(self.window)

        self.favorites_box = self.builder.get_object('favoritesBox')

        self.windows_to_restore = []

        self.child_at_pos = None
        # This is a list of Monitor objects
        self.monitors = MonitorParser.build_monitors_from_gdk()
        if not self.monitors:
            self.errorDialog.set_markup(
                '''
<b>Oh noes! 😱</b>

There was an error parsing your monitors!
Make sure that you're running HydraPaper using the flatpak version, or otherwise that you have Gtk and Gdk version >=3.22 installed in your system.

If you\'re still experiencing problems, considering filling an issue <a href="https://github.com/gabmus/hydrapaper/issues">on HydraPaper\'s bugtracker</a>, running HydraPaper from your terminal and including the output log.
                '''
            )
            self.errorDialog.run()
            exit(1)
        self.sync_monitors_from_config()
        self.wallpapers_list = []

        self.wallpapers_folders_toggle = self.builder.get_object('wallpapersFoldersToggle')
        self.wallpapers_folders_popover = self.builder.get_object('wallpapersFoldersPopover')
        self.wallpapers_folders_popover_listbox = self.builder.get_object('wallpapersFoldersPopoverListbox')

    def on_window_size_allocate(self, *args):
        alloc = self.window.get_allocation()
        self.configuration['windowsize']['width'] = alloc.width
        self.configuration['windowsize']['height'] = alloc.height

    def do_before_quit(self):
        self.unminimize_all_other_windows()
        self.save_config_file()

    def sync_monitors_from_config(self):
        for m in self.monitors:
            if m.name in self.configuration['monitors'].keys():
                m.wallpaper = self.configuration['monitors'][m.name]
            else:
                self.configuration['monitors'][m.name] = m.wallpaper
        self.save_config_file(self.configuration)

    def dump_monitors_to_config(self):
        for m in self.monitors:
            if m.name in self.configuration['monitors'].keys():
                self.configuration['monitors'][m.name] = m.wallpaper
        self.save_config_file(self.configuration)

    def save_config_file(self, n_config=None):
        if not n_config:
            n_config = self.configuration
        with open(self.CONFIG_FILE_PATH, 'w') as fd:
            fd.write(json.dumps(n_config))
            fd.close()

    def get_config_file(self):
        if not os.path.isfile(self.CONFIG_FILE_PATH):
            n_config = {
                'wallpapers_paths': [
                    {
                        'path': '{0}/Pictures'.format(HOME),
                        'active': True
                    },
                    {
                        'path': '/usr/share/backgrounds/gnome/',
                        'active': True
                    }
                ],
                'selection_mode': 'single',
                'monitors': {},
                'favorites': [],
                'favorites_in_mainview': False,
                'windowsize': {
                    'width': 600,
                    'height': 400
                },
            }
            self.save_config_file(n_config)
            return n_config
        else:
            do_save = False
            with open(self.CONFIG_FILE_PATH, 'r') as fd:
                config = json.loads(fd.read())
                fd.close()
                if not 'wallpapers_paths' in config.keys():
                    config['wallpapers_paths'] = [
                    {
                        'path': '{0}/Pictures'.format(HOME),
                        'active': True
                    },
                    {
                        'path': '/usr/share/backgrounds/gnome/',
                        'active': True
                    }
                ]
                    do_save = True
                if len(config['wallpapers_paths']) > 0:
                    for index, path in enumerate(config['wallpapers_paths']):
                        if type(path) == str:
                            config['wallpapers_paths'][index] = {
                                'path': path,
                                'active': True
                            }
                    do_save = True
                if not 'selection_mode' in config.keys():
                    config['selection_mode'] = 'single'
                    do_save = True
                if not 'monitors' in config.keys():
                    config['monitors'] = {}
                    do_save = True
                if not 'favorites' in config.keys():
                    config['favorites'] = []
                    do_save = True
                if not 'favorites_in_mainview' in config.keys():
                    config['favorites_in_mainview'] = False
                    do_save = True
                if not 'windowsize' in config.keys():
                    config['windowsize'] = {
                        'width': 600,
                        'height': 400
                    }
                    do_save = True
                if do_save:
                    self.save_config_file(config)
                return config

    def remove_wallpaper_folder(self, btn):
        row=self.wallpapers_folders_popover_listbox.get_selected_row()
        if not row:
            return
        if not row.value:
            return
        for index, path in enumerate(self.configuration['wallpapers_paths']):
            if path['path'] == row.value:
                self.configuration['wallpapers_paths'].pop(index)
                break
        self.save_config_file()
        self.fill_wallpapers_folders_popover_listbox()
        self.refresh_wallpapers_flowbox()

    def all_wallpaper_folder_interactives_set_sensitive(self, sensitive):
        # listbox
        # --> listboxrow []
        #     --> box
        #         --> checkbutton
        #         --> label
        #         --> button
        for child in self.wallpapers_folders_popover_listbox.get_children():
            for subchild in child.get_child().get_children():
                if type(subchild) in [Gtk.CheckButton, Gtk.Button]:
                    subchild.set_sensitive(sensitive)
        self.add_to_favorites_toggle.set_sensitive(sensitive)
        self.builder.get_object('addWallpapersPath').set_sensitive(sensitive)
        self.on_wallpapersFoldersPopoverListbox_row_selected(
            self.wallpapers_folders_popover_listbox,
            self.wallpapers_folders_popover_listbox.get_selected_row()
        )


    def on_wallpaper_folder_switch_toggled(self, check, state):
        if not check.value:
            return
        for index, folder in enumerate(self.configuration['wallpapers_paths']):
            if folder['path'] == check.value:
                self.configuration['wallpapers_paths'][index]['active'] = check.get_active()
                break
        self.save_config_file()
        #self.refresh_wallpapers_flowbox()
        self.show_hide_wallpapers()

    def fill_wallpapers_folders_popover_listbox(self):
        ListboxHelper.empty_listbox(self.wallpapers_folders_popover_listbox)
        for folder in self.configuration['wallpapers_paths']:
            self.wallpapers_folders_popover_listbox.add(
                WallpapersFolderListBoxRow.WallpapersFolderListBoxRow(
                    folder['path'],
                    folder['active'],
                    self.on_wallpaper_folder_switch_toggled
                )
            )
        self.wallpapers_folders_popover_listbox.show_all()

    def set_monitor_wallpaper_preview(self, wp_path):
        monitor_widgets = self.monitors_flowbox.get_selected_children()[0].get_children()[0].get_children()
        for w in monitor_widgets:
            if type(w) == Gtk.Image:
                m_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(wp_path, 64, 64, True)
                w.set_from_pixbuf(m_pixbuf)
            elif type(w) == Gtk.Label:
                current_m_name = w.get_text()
                for m in self.monitors:
                    if m.name == current_m_name:
                        m.wallpaper = wp_path

    def make_monitors_flowbox_item(self, monitor):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        label = Gtk.Label()
        label.set_text(monitor.name)
        image = Gtk.Image()
        if monitor.wallpaper and self.check_if_image(monitor.wallpaper):
            m_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(monitor.wallpaper, 64, 64, True)
            image.set_from_pixbuf(m_pixbuf)
        else:
            image.set_from_icon_name('image-missing', Gtk.IconSize.DIALOG)
        box.pack_start(image, False, False, 0)
        box.pack_start(label, False, False, 0)
        box.set_margin_left(24)
        box.set_margin_right(24)
        return box

    def make_wallpapers_flowbox_item(self, wp_path):
        return WallpaperFlowboxItem.WallpaperBox(wp_path)

    def fill_monitors_flowbox(self):
        for m in self.monitors:
            self.monitors_flowbox.insert(
                self.make_monitors_flowbox_item(m),
            -1) # -1 appends to the end

    def evaluate_wallpaper_visibility(self, wp_widget, flowbox):
        visibility = False
        exists_in_folder = False
        for folder in self.configuration['wallpapers_paths']:
            if folder['path'] in wp_widget.wallpaper_path:
                exists_in_folder = True
                if folder['active']:
                    visibility = True
                else:
                    return False
                break
        if exists_in_folder:
            visibility = True
        else:
            return False
        if flowbox == self.wallpapers_flowbox:
            if wp_widget.wallpaper_path in self.configuration['favorites']:
                if self.configuration['favorites_in_mainview']:
                    visibility = True
                else:
                    return False
            else:
                visibility = True
        else:
            if wp_widget.wallpaper_path in self.configuration['favorites']:
                visibility = True
            else:
                return False
        return visibility

    def show_hide_wallpapers(self):
        for wp_widget in self.wallpapers_flowbox.get_children():
            if self.evaluate_wallpaper_visibility(wp_widget, self.wallpapers_flowbox):
                wp_widget.show_all()
            else:
                wp_widget.hide()
        for wp_widget in self.wallpapers_flowbox_favorites.get_children():
            if self.evaluate_wallpaper_visibility(wp_widget, self.wallpapers_flowbox_favorites):
                wp_widget.show_all()
            else:
                wp_widget.hide()

    def fill_wallpapers_flowbox(self): # called by self.refresh_wallpapers_flowbox
        for w in self.wallpapers_list:
            if self.check_if_image(w):
                widget = self.make_wallpapers_flowbox_item(w)
                if w in self.configuration['favorites']:
                    widget.set_fav(True)
                else:
                    widget.set_fav(False)
                self.wallpapers_flowbox.insert(widget, -1) # -1 appends to the end
                if w in self.configuration['favorites']:
                    widget_c = self.make_wallpapers_flowbox_item(w)
                    widget_c.set_fav(True)
                    self.wallpapers_flowbox_favorites.insert(widget_c, -1)
                    widget_c.show_all()
                    self.wallpapers_flowbox_favorites.show_all()
                widget.show_all()
                self.wallpapers_flowbox.show_all()
        for wb in self.wallpapers_flowbox_favorites.get_children():
            wb.set_wallpaper_thumb()
        for wb in self.wallpapers_flowbox.get_children():
            wb.set_wallpaper_thumb()

    def check_if_image(self, pic):
        im_path = pathlib.Path(pic)
        return (
            im_path.suffix.lower() in IMAGE_EXTENSIONS and
            im_path.exists() and
            not im_path.is_dir()
        )

    def get_wallpapers_list(self, *args):
        for path_dict in self.configuration['wallpapers_paths']:
            folder = path_dict['path']
            if os.path.isdir(folder): # trying to just hide wallpapers in non active paths # and path_dict['active']:
                pictures = os.listdir(folder)
                for pic in pictures:
                    picpath = '{0}/{1}'.format(folder, pic)
                    if not self.check_if_image(picpath):
                        pictures.pop(pictures.index(pic))
                self.wallpapers_list.extend(['{0}/'.format(folder) + pic for pic in pictures])

    def empty_wallpapers_flowbox(self):
        self.wallpapers_list = []
        while True:
            item = self.wallpapers_flowbox.get_child_at_index(0)
            if item:
                self.wallpapers_flowbox.remove(item)
                item.destroy()
            else:
                break
        while True:
            item = self.wallpapers_flowbox_favorites.get_child_at_index(0)
            if item:
                self.wallpapers_flowbox_favorites.remove(item)
                item.destroy()
            else:
                break

    def refresh_wallpapers_flowbox(self):
        if self.wallpapers_refreshing_locked:
            return
        self.wallpapers_refreshing_locked = True
        self.all_wallpaper_folder_interactives_set_sensitive(False)
        self.empty_wallpapers_flowbox()
        # if len(self.configuration['favorites']) == 0:
        #     self.favorites_box.hide()
        # else:
        #     self.favorites_box.show_all()
        get_wallpapers_thread = ThreadingHelper.do_async(self.get_wallpapers_list, (0,))
        ThreadingHelper.wait_for_thread(get_wallpapers_thread)
        self.fill_wallpapers_flowbox()
        self.show_hide_wallpapers()
        self.wallpapers_refreshing_locked = False
        self.all_wallpaper_folder_interactives_set_sensitive(True)

    def do_activate(self):
        self.add_window(self.window)
        self.window.set_wmclass('HydraPaper', 'HydraPaper')
        # self.window.set_title('HydraPaper')

        appMenu = Gio.Menu()
        appMenu.append("About", "app.about")
        appMenu.append("Settings", "app.settings")
        appMenu.append("Quit", "app.quit")

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self.on_about_activate)
        self.builder.get_object("aboutdialog").connect(
            "delete-event", lambda *_:
                self.builder.get_object("aboutdialog").hide() or True
        )
        self.add_action(about_action)

        settings_action = Gio.SimpleAction.new("settings", None)
        settings_action.connect("activate", self.on_settings_activate)
        self.builder.get_object("settingsWindow").connect(
            "delete-event", lambda *_:
                self.builder.get_object("settingsWindow").hide() or True
        )
        self.add_action(settings_action)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self.on_quit_activate)
        self.add_action(quit_action)
        self.set_app_menu(appMenu)

        self.fill_monitors_flowbox()
        self.fill_wallpapers_folders_popover_listbox()

        self.window.show_all()

        self.refresh_wallpapers_flowbox()

    def do_command_line(self, args):
        """
        GTK.Application command line handler
        called if Gio.ApplicationFlags.HANDLES_COMMAND_LINE is set.
        must call the self.do_activate() to get the application up and running.
        """
        Gtk.Application.do_command_line(self, args)  # call the default commandline handler
        # make a command line parser
        parser = argparse.ArgumentParser(prog='gui')
        # add a -c/--color option
        parser.add_argument('-q', '--quit-after-init', dest='quit_after_init', action='store_true', help='initialize application (e.g. for macros initialization on system startup) and quit')
        # parse the command line stored in args, but skip the first element (the filename)
        self.args = parser.parse_args(args.get_arguments()[1:])
        # call the main program do_activate() to start up the app
        self.do_activate()
        return 0

    def on_about_activate(self, *args):
        self.builder.get_object("aboutdialog").show()

    def on_settings_activate(self, *args):
        self.builder.get_object("settingsWindow").show()

    def on_quit_activate(self, *args):
        self.do_before_quit()
        self.quit()

    def onDeleteWindow(self, *args):
        self.do_before_quit()
        self.quit()

    # Handler functions START

    def on_wallpapersFlowbox_rightclick_or_longpress(self, gesture_or_event, x, y, flowbox):
        self.child_at_pos = flowbox.get_child_at_pos(x,y)
        if not self.child_at_pos:
            return
        self.wallpapers_flowbox_itemoptions_popover.set_relative_to(self.child_at_pos)
        flowbox.select_child(self.child_at_pos)
        if flowbox == self.wallpapers_flowbox_favorites or self.child_at_pos.is_fav:
            self.add_to_favorites_toggle.set_label('💔 Remove from favorites')
        else:
            self.add_to_favorites_toggle.set_label('❤️ Add to favorites')
        wp_path = self.child_at_pos.get_child().wallpaper_path
        self.selected_wallpaper_path_entry.set_text(wp_path)
        self.builder.get_object('selectedWallpaperName').set_text(pathlib.Path(wp_path).name)
        self.on_wallpapersFlowbox_child_activated(flowbox, self.child_at_pos)
        self.wallpapers_flowbox_itemoptions_popover.popup()

    def on_wallpapersFlowbox_button_release_event(self, flowbox, event):
        if event.button == 3: # 3 is the right mouse button
            self.on_wallpapersFlowbox_rightclick_or_longpress(
                event,
                event.x,
                event.y,
                flowbox
            )

    def on_aboutdialog_close(self, *args):
        self.builder.get_object("aboutdialog").hide()

    def on_wallpapersFlowbox_child_activated(self, flowbox, selected_item):
        self.set_monitor_wallpaper_preview(
            selected_item.get_child().wallpaper_path
        )

    def apply_button_async_handler(self, monitors):
        desktop_environment = os.environ.get('XDG_CURRENT_DESKTOP')
        if desktop_environment == 'MATE':
            wp_setter_func = WallpaperMerger.set_wallpaper_mate
        else:
            wp_setter_func = WallpaperMerger.set_wallpaper_gnome
        if len(monitors) == 1:
            wp_setter_func(monitors[0].wallpaper, 'zoom')
            return
        #if len(self.monitors) != 2:
        #    print('Configurations different from 2 monitors are not supported for now :(')
        #    exit(1)
        if not os.path.isdir(HYDRAPAPER_CACHE_PATH):
            os.mkdir(HYDRAPAPER_CACHE_PATH)
        new_wp_filename = '_'.join(([m.__repr__() for m in monitors]))
        saved_wp_path = '{0}/{1}.png'.format(HYDRAPAPER_CACHE_PATH, hashlib.sha256(
            'HydraPaper{0}'.format(new_wp_filename).encode()
        ).hexdigest())
        if not os.path.isfile(saved_wp_path):
            WallpaperMerger.multi_setup_pillow(
                monitors,
                saved_wp_path
            )
        else:
            print(
                'Hit cache for wallpaper {0}. Skipping merge operation.'.format(
                    saved_wp_path
                )
            )
        wp_setter_func(saved_wp_path)

    def set_favorite_state(self, wp_path, wp_widget, isfavorite):
        if isfavorite:
            widget_c = self.make_wallpapers_flowbox_item(wp_path)
            widget_c.set_fav(True)
            wp_widget.set_fav(True)
            self.wallpapers_flowbox_favorites.insert(widget_c, -1)
            widget_c.show_all()
            self.wallpapers_flowbox_favorites.show_all()
            widget_c.set_wallpaper_thumb()
        else:
            for wb in self.wallpapers_flowbox_favorites.get_children():
                if wb.wallpaper_path == wp_path:
                    self.wallpapers_flowbox_favorites.remove(wb)
                    wb.destroy()
                    break
            for wb in self.wallpapers_flowbox.get_children():
                if wb.wallpaper_path == wp_path:
                    wb.set_fav(False)
                    break
        self.show_hide_wallpapers()

    def on_wallpapersFlowboxItemoptionsPopover_notify_visible(self, *args):
        if self.favorites_button_clicked:
            button = self.add_to_favorites_toggle
            if not self.child_at_pos:
                return
            wp_path = self.child_at_pos.get_child().wallpaper_path
            if 'add' in button.get_label().lower():
                self.configuration['favorites'].append(wp_path)
            else:
                self.configuration['favorites'].pop(self.configuration['favorites'].index(wp_path))
            self.save_config_file()
            self.wallpapers_flowbox_itemoptions_popover.set_relative_to(self.wallpapers_flowbox)
            self.set_favorite_state(wp_path, self.child_at_pos, 'add' in button.get_label().lower())
            self.favorites_button_clicked = False

    def on_addToFavoritesToggle_clicked(self, button):
        self.favorites_button_clicked = True
        self.wallpapers_flowbox_itemoptions_popover.popdown()

    def on_applyButton_clicked(self, btn):
        for m in self.monitors:
            if not m.wallpaper:
                print('Set all of the wallpapers before applying')
                self.errorDialog.set_markup('Set all of the wallpapers before applying')
                self.errorDialog.run()
                self.errorDialog.hide()
                return
        # disable interaction
        self.apply_button.set_sensitive(False)
        self.monitors_flowbox.set_sensitive(False)
        self.wallpapers_flowbox.set_sensitive(False)
        # activate spinner
        self.apply_spinner.show()
        self.apply_spinner.start()
        # run thread
        thread = ThreadingHelper.do_async(self.apply_button_async_handler, (self.monitors[:],))
        # wait for thread to finish
        ThreadingHelper.wait_for_thread(thread)
        # restore interaction and deactivate spinner
        self.apply_button.set_sensitive(True)
        self.monitors_flowbox.set_sensitive(True)
        self.wallpapers_flowbox.set_sensitive(True)
        self.apply_spinner.stop()
        self.apply_spinner.hide()
        self.dump_monitors_to_config()

    def on_wallpapersFoldersToggle_toggled(self, toggle):
        if toggle.get_active():
            self.wallpapers_folders_popover.popup()
        else:
            self.wallpapers_folders_popover.popdown()

    def on_wallpapersFoldersPopover_closed(self, popover):
        self.wallpapers_folders_toggle.set_active(False)

    def add_new_wallpapers_path(self, new_path):
        self.configuration['wallpapers_paths'].append(
            {
                'path': new_path,
                'active': True
            }
        )
        self.save_config_file()
        self.fill_wallpapers_folders_popover_listbox()
        self.refresh_wallpapers_flowbox()

    def on_wallpaperSelectionModeToggle_state_set(self, switch, doubleclick_activate):
        if doubleclick_activate:
            self.configuration['selection_mode'] = 'double'
        else:
            self.configuration['selection_mode'] = 'single'
        self.wallpapers_flowbox.set_activate_on_single_click(not doubleclick_activate)
        self.wallpapers_flowbox_favorites.set_activate_on_single_click(not doubleclick_activate)
        self.save_config_file(self.configuration)

    def on_keepFavoritesInMainviewToggle_state_set(self, switch, favs_in_mainview):
        if self.configuration['favorites_in_mainview'] != favs_in_mainview:
            self.configuration['favorites_in_mainview'] = favs_in_mainview
            self.save_config_file(self.configuration)
            self.show_hide_wallpapers()

    def on_resetFavoritesButton_clicked(self, button):
        self.configuration['favorites'] = []
        self.save_config_file()
        self.refresh_wallpapers_flowbox()

    def unminimize_all_other_windows(self):
        from time import time as timestamp
        screen = Wnck.Screen.get_default()
        screen.force_update()  # recommended per Wnck documentation
        for window in self.windows_to_restore:
            if window.is_minimized():
                window.activate(timestamp())
        for window in screen.get_windows():
            if window.get_application().get_name() == 'hydrapaper':
                window.activate(timestamp())
                break

    def on_lowerAllOtherWindowsToggle_toggled(self, toggle):
        if toggle.get_active():
            self.builder.get_object('lowerAllOtherWindowsToggle').get_child().set_from_icon_name('go-top-symbolic', Gtk.IconSize.BUTTON)
            screen = Wnck.Screen.get_default()
            screen.force_update()  # recommended per Wnck documentation
            self.windows_to_restore = []
            for window in screen.get_windows():
                if not window.is_minimized() and not 'desktop' in window.get_application().get_name().lower() and window.get_application().get_name() != 'hydrapaper':
                    self.windows_to_restore.append(window)
                    window.minimize()
        else:
            self.builder.get_object('lowerAllOtherWindowsToggle').get_child().set_from_icon_name('go-bottom-symbolic', Gtk.IconSize.BUTTON)
            self.unminimize_all_other_windows()

    def on_addWallpapersPath_clicked(self, button):
        self.builder.get_object('pathAlreadyAddedInfobarLikeRevealer').set_reveal_child(False)
        self.builder.get_object('addFolderFileChooserDialog').run()

    def on_addFolderFileChooserDialogCancelButton_clicked(self, button):
        self.builder.get_object('addFolderFileChooserDialog').hide()
        self.builder.get_object('pathAlreadyAddedInfobarLikeRevealer').set_reveal_child(False)

    def wallpaper_path_exists(self, folder):
        for wp in self.configuration['wallpapers_paths']:
            if folder == wp['path']:
                return True
        return False

    def on_addFolderFileChooserDialogOpenButton_clicked(self, button):
        new_path = self.builder.get_object('addFolderFileChooserDialog').get_filename()
        if os.path.isdir(new_path):
            if not self.wallpaper_path_exists(new_path):
                self.builder.get_object('addFolderFileChooserDialog').hide()
                self.builder.get_object('pathAlreadyAddedInfobarLikeRevealer').set_reveal_child(False)
                self.add_new_wallpapers_path(new_path)
            else:
                self.builder.get_object('pathAlreadyAddedInfobarLikeRevealer').set_reveal_child(True)

    def on_pathAlreadyAddedInfobarLikeRevealerCloseButton_clicked(self, button):
        self.builder.get_object('pathAlreadyAddedInfobarLikeRevealer').set_reveal_child(False)

    def on_wallpapersFoldersPopoverListbox_row_selected(self, listbox, row):
        self.builder.get_object('removeWallpapersPath').set_sensitive(not not row and self.builder.get_object('addWallpapersPath').get_sensitive())

    # Handler functions END

def main():
    application = Application()

    try:
        ret = application.run(sys.argv)
    except SystemExit as e:
        ret = e.code

    sys.exit(ret)


if __name__ == '__main__':
    main()
