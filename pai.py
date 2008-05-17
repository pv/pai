#!/usr/bin/env python
"""
===
pai
===

-------------------------
Picture archive inspector
-------------------------

:Author: Pauli Virtanen <pav@iki.fi>
"""
from __future__ import division

__version__ = "0.1"

# Sorry, the code is slowly becoming a mess...

import sys, os, shutil, tempfile, re, random, time, traceback, copy
import pygtk
pygtk.require('2.0')
import gtk
import gobject
import gc
import traceback
import optparse

import threading, Queue

IMAGE_EXTENSIONS = [ '.jpg', '.gif', '.png', '.tif', '.tiff', '.bmp' ]

##############################################################################
## Detect Maemo/Hildon
##############################################################################

try:
    import hildon
    HILDON = True
except ImportError:
    HILDON = False

if HILDON:
    DEFAULT_COLUMNS = 1
    MAX_IMAGE_CACHE = 2
    DO_PRELOADING = True
else:
    DEFAULT_COLUMNS = 2
    MAX_IMAGE_CACHE = 10
    DO_PRELOADING = True

##############################################################################
## GUI threading helpers
##############################################################################
#
# Every time you call a non-threadsafe GTK function (=most of them),
# be sure that either
#
# 1. The function where you make the call is a GTK signal handler, OR,
# 2. The function is in GUI thread and holding the GDK lock, OR,
# 3. You make the call via run_in_gui_thread or run_later_in_gui_thread
#
# Note: gobject.* callback functions don't reserve the GDK lock.
#       To avoid confusion, ONLY USE THE run_* FUNCTIONS BELOW.
#

#
# Blocks to run in gui thread sometime later (as a shorthand) can be
# specified via
#
# >>> @run_in_gui_thread
# ... def _():
# ...     stuff_to_do
#
# But remember how lexical binding works though,
#
# >>> item = True
# >>> def foo():
# ...     assert item == True
# >>> item = False
# >>> foo()
# Traceback (most recent call last):
#   ...
# AssertionError
#

def run_in_gui_thread(func, *a, **kw):
    """Run the function in the GUI thread, next time when the GUI is idle."""
    # NB: gobject.idle_add functions are in main thread, but NOT inside
    #     the GDK lock
    def timer():
        gtk.gdk.threads_enter()
        try:
            func(*a, **kw)
            return False
        finally:
            gtk.gdk.threads_leave()
    gobject.idle_add(timer)
    return None

def run_later_in_gui_thread(delay, func, *a, **kw):
    """Run the function in the GUI thread, after a delay"""
    # NB: gobject.idle_add functions are in main thread, but NOT inside
    #     the GDK lock
    def timer():
        gtk.gdk.threads_enter()
        try:
            func(*a, **kw)
            return False
        finally:
            gtk.gdk.threads_leave()
    gobject.timeout_add(delay, timer)
    return None

def assert_gui_thread(func):
    """Assert that this function is ran in the GUI thread. [decorator]"""
    if not __debug__: return func
    def _wrapper(*a, **kw):
        assert threading.currentThread() == threading.enumerate()[0], \
               (func, threading.currentThread(), threading.enumerate())
        return func(*a, **kw)
    return _wrapper

##############################################################################
## Image list / recursive archive unpack
##############################################################################

import zipfile
import tarfile
import subprocess

class ExtensionMap(dict):
    def __init__(self, dictionary=None):
        dict.__init__(self)
        self.update(dictionary)
        
    def has_key(self, string):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return True
        return False

    def get(self, string, default=None):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return dict.__getitem__(self, key)
        return default

    def __contains__(self, string):
        return self.has_key(string)

    def __getitem__(self, string):
        for key in self.iterkeys():
            if string.lower().endswith(key):
                return dict.__getitem__(self, key)
        raise KeyError(string)

def numeric_file_sort(filelist):
    """Return the given list, sorted by file name, not heeding
       leading zeroes in numbers."""
    
    def sort_key(filename):
        key = ""
        lastend = 0
        for match in re.finditer("\d+", filename):
            key += filename[lastend:match.start()]
            lastend = match.end()
            key += "%08d" % (int(match.group()))
        return key

    lst = list(filelist)
    lst.sort(key=sort_key)
    return lst

class DummyUnpacker(object):
    def __init__(self, archive_filename):
        self.archive = archive_filename
        self._files = None

    @property
    def files(self):
        """Return a list of full pathnames to the archive"""
        if self._files is None:
            self._files = self._get_files()
        return self._files

    def _get_files(self):
        return [self.archive]

    def open_file(self, name):
        """Open a file in the archive"""
        return open(self.archive, 'r')

    def _prefix_archive(self, lst):
        return [self.archive + os.path.sep + fn for fn in lst]

    def _unprefix_archive(self, name):
        if not os.path.commonprefix([name, self.archive]) == self.archive:
            raise ValueError("File not in archive!")
        return name[len(self.archive)+1:]
    
class ZipUnpacker(DummyUnpacker):
    def _get_files(self):
        f = zipfile.ZipFile(self.archive, 'r')
        try:
            return self._prefix_archive(f.namelist())
        finally:
            f.close()

    def open_file(self, name):
        name = self._unprefix_archive(name)
        f = zipfile.ZipFile(self.archive, 'r')
        try:
            return f.read(name)
        finally:
            f.close()

class TarUnpacker(DummyUnpacker):
    def _get_files(self):
        f = tarfile.open(self.archive_filename, 'r')
        try:
            return self._prefix_archive(f.getnames())
        finally:
            f.close()

    def open_file(self, name):
        name = self._unprefix_archive(name)
        f = tarfile.open(self.archive_filename, 'r')
        try:
            return f.extractfile(name).read()
        except:
            f.close()

class RarUnpacker(DummyUnpacker):
    def _get_files(self):
        p = subprocess.Popen(["unrar", "vb", self.archive],
                             stdout=subprocess.PIPE,
                             stdin=subprocess.PIPE)
        out, err = p.communicate()
        lst = [fn.strip() for fn in out.split("\n") if fn.strip()]
        return self._prefix_archive(lst)
    
    def open_file(self, name):
        tmpdir = tempfile.mkdtemp()
        name = self._unprefix_archive(name)

        p = subprocess.Popen(["unrar", "e", self.archive, name,
                              tmpdir + os.path.sep],
                             stdout=subprocess.PIPE, stdin=subprocess.PIPE)
        p.communicate()
        
        try:
            basename = os.path.basename(name)
            return open(os.path.join(tmpdir, basename), 'r')
        finally:
            shutil.rmtree(tmpdir)

def recursive_find(dirname, unpackers, progress_queue=None):
    """Return all files under dirname, with associated unpackers (if any)."""
    pathlist = [ os.path.abspath(dirname) ]
    files = []
    file_unpackers = {}
    
    while len(pathlist) > 0:
        path = pathlist[0]
        del pathlist[0]
        
        if os.path.isdir(path):
            if progress_queue: progress_queue.put(os.path.basename(path))

            for fn in reversed(numeric_file_sort([os.path.join(path, i)
                                                  for i in os.listdir(path)])):
                pathlist.insert(0, fn)
        elif os.path.isfile(path) and path in unpackers:
            if progress_queue: progress_queue.put(os.path.basename(path))
            unpacker = unpackers[path](os.path.abspath(path))

            add_list = numeric_file_sort(unpacker.files)
            for fn in add_list:
                file_unpackers[fn] = unpacker
            files += add_list
        else:
            files.append(path)
    
    return files, file_unpackers

class FileList(object):
    """Get a list of files in given sources, including contents of archives,
    which will be recursively unpacked."""
    
    zip_extension_map = ExtensionMap({
        '.zip': ZipUnpacker,
        '.tar': TarUnpacker,
        '.tar.gz': TarUnpacker,
        '.tar.bz2': TarUnpacker,
        '.tbz': TarUnpacker,
        '.tb2': TarUnpacker,
        '.tgz': TarUnpacker,
        '.rar': RarUnpacker,
        })

    def __init__(self, filenames, extensionlist=None, progress_queue=None):
        if not isinstance(filenames, list):
            filenames = [filenames]
        
        # Recursive find
        self._files = []
        self._file_unpackers = {}
        for filename in filenames:
            fns, ups = recursive_find(filename, FileList.zip_extension_map,
                                      progress_queue)
            self._files += fns
            self._file_unpackers.update(ups)
        
        if extensionlist:
            self._files = [i for i in self._files
                           if os.path.splitext(i)[1].lower() in extensionlist]

    def __str__(self):
        return str(self._files)
    
    def __getitem__(self, i):
        return self._files[i]

    def __len__(self):
        return len(self._files)

    def open_file(self, fn):
        unpacker = self._file_unpackers.get(fn)
        if unpacker:
            return unpacker.open_file(fn)
        else:
            return open(fn, 'r')

##############################################################################
## ImageCache / ImageView
##############################################################################

class ImageCache(object):
    def __init__(self, filelist, max_items=MAX_IMAGE_CACHE):
        self.raw_pixbufs = {}
        self.scaled_pixbufs = {}
        self.filenames = []
        self.filelist = filelist
        self.max_items = max_items
        self.interpolation = gtk.gdk.INTERP_BILINEAR

    @assert_gui_thread
    def add(self, filename):
        if filename in self.filenames:
            return # noop
        
        # cleanup cache, if necessary
        if len(self.filenames) + 1 > self.max_items:
            try:
                del self.raw_pixbufs[self.filenames[0]]
            except KeyError:
                pass
            try:
                del self.scaled_pixbufs[self.filenames[0]]
            except KeyError:
                pass
            del self.filenames[0]
# FIXME: for some reason the following gc.collect() wreaks havoc on
#        pygtk 2.11.0-0ubuntu1 (worked on 2.10.4), and results to PaiUI
#        losing its __dict__! I have no clue what's going on.
#
#            gc.collect()

        # load image (aargh, gtk.gdk.PixbufLoader doesn't work properly...)
        if os.path.isfile(filename):
            raw_pixbuf = gtk.gdk.pixbuf_new_from_file(filename)
        else:
            f=tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1])
            fh = self.filelist.open_file(filename)
            if isinstance(fh, str):
                f.write(fh)
            else:
                try:
                    shutil.copyfileobj(fh, f)
                finally:
                    fh.close()
            f.flush()
            raw_pixbuf = gtk.gdk.pixbuf_new_from_file(f.name)
            f.close()

        # process
        self.filenames.append(filename)
        self.raw_pixbufs[filename] = raw_pixbuf

    def set_interpolation(self, interpolation):
        self.scaled_pixbufs = {}
        self.interpolation = interpolation

    def get(self, filename):
        self.add(filename)
        return self.raw_pixbufs[filename]

    @assert_gui_thread
    def get_scaled(self, filename, width, height, rotated=False):
        self.add(filename)
        try:
            pixbuf = self.scaled_pixbufs[filename]
            if pixbuf.get_width() != width or pixbuf.get_height() != height:
                raise KeyError(filename)
            return pixbuf
        except KeyError:
            pixbuf = self.raw_pixbufs[filename]
            if rotated:
                pixbuf = pixbuf.rotate_simple(
                    gtk.gdk.PIXBUF_ROTATE_CLOCKWISE)
            if width != pixbuf.get_width() or height != pixbuf.get_height():
                pixbuf = pixbuf.scale_simple(width, height,
                                             self.interpolation)
            self.scaled_pixbufs[filename] = pixbuf
# FIXME: same problem with gc.collect() as above!
#
#            gc.collect()
            return pixbuf

class ImageView(gtk.DrawingArea):
    __gsignals__ = {
        'expose-event': 'override',
        'style-set': 'override',
        'direction-changed': 'override',
        }

    def __init__(self, cache, xspacing=0):
        self.xspacing = xspacing
        self.cache = cache

        self.pango_context = None
        self.pango_layout = None

        self.filenames = []

        self.text = u""

        self.rotated = False
        self.zoom_ratio = 1.0
        self.offset = [0, 0]
        self.limits = [0, 0]
        self.screen_size = [0, 0]

        gtk.DrawingArea.__init__(self)
        style = self.get_style().copy()
        style.bg[gtk.STATE_NORMAL] = gtk.gdk.Color(0, 0, 0)
        self.set_style(style)

    def normalize_offset(self):
        def limit(x, a, b):
            return min(max(x, a), b)
        for j in 0, 1:
            self.offset[j] = limit(
                self.offset[j],
                min(0, -self.limits[j]/2 + self.screen_size[j]/2),
                max(0, +self.limits[j]/2 - self.screen_size[j]/2))

    def set_files(self, filenames):
        self.filenames = filenames
        run_in_gui_thread(self.queue_resize)

    @assert_gui_thread
    def do_style_set(self, previous_style):
        if self.pango_layout:
            self.pango_layout.context_changed()
        if isinstance(previous_style, gtk.Style):
            gtk.Widget.do_style_set(self, previous_style)

    @assert_gui_thread
    def do_direction_changed(self, direction):
        if self.pango_layout:
            self.pango_layout.context_changed()
        gtk.Widget.do_direction_changed(self, direction)

    @assert_gui_thread
    def do_expose_event(self, event):
        if not self.window or not self.window.is_visible():
            return False
        if not self.cache or not self.filenames:
            return False
        x, y, width, height = self.get_allocation()

        # draw some text
        text_size = self.__get_text_size_and_prepare_layout()
        height -= text_size[1]
        
        self.window.draw_layout(self.get_style().white_gc, 0, 0,
                                self.pango_layout,
                                background=gtk.gdk.Color(0,0,0),
                                foreground=gtk.gdk.Color(65535,65535,65535))
 
        # render image
        self.normalize_offset()
        to_show = self.__get_files_to_show(self.filenames, width, height)
        for xpos, ypos, pixbuf in to_show:
            self.blit_image(pixbuf,
                            xpos - self.offset[0],
                            ypos - self.offset[1] + text_size[1],
                            event.area)
        return False
    
    @assert_gui_thread
    def preload(self, filenames):
        if not DO_PRELOADING:
            return
        
        if not self.window or not self.window.is_visible():
            return
        
        if not isinstance(filenames, list):
            filenames = [filenames]
        x, y, width, height = self.get_allocation()
        text_size = self.__get_text_size_and_prepare_layout()
        height -= text_size[1]
        self.__get_files_to_show(filenames, width, height)

    @assert_gui_thread
    def __get_text_size_and_prepare_layout(self):
        if not self.pango_context:
            self.pango_context = self.create_pango_context()
        if not self.pango_layout:
            self.pango_layout = self.create_pango_layout(self.text)
        self.pango_layout.set_text(self.text)
        text_size = self.pango_layout.get_pixel_size()
        return text_size

    @assert_gui_thread
    def __get_files_to_show(self, files, win_width, win_height):
        xpos = []
        ypos = []
        pixbufs = []

        total_width = 0
        total_height = 0

        i = 0
        while i < len(files):
            try:
                raw_pixbuf = self.cache.get(files[i])
            except IOError:
                del files[i]
                continue
            total_width += raw_pixbuf.get_width()
            total_height = max(raw_pixbuf.get_height(), total_height)
            i += 1

        if len(files) == 0:
            return []
        
        # FIXME: separate layout rotation from image rotation?
        if self.rotated:
            total_width, total_height = total_height, total_width

        available_width = win_width - self.xspacing*(len(files) - 1)
        available_height = win_height

        ratio = min(available_width / total_width,
                    available_height / total_height)
        ratio *= self.zoom_ratio

        for i in range(len(files)):
            raw_pixbuf = self.cache.get(files[i])
            img_width = int(raw_pixbuf.get_width() * ratio)
            img_height = int(raw_pixbuf.get_height() * ratio)

            if self.rotated:
                img_width, img_height = img_height, img_width

            pixbuf = self.cache.get_scaled(files[i], img_width, img_height,
                                           self.rotated)

            # FIXME: separate layout rotation from image rotation?
            if not self.rotated:
                if not xpos:
                    xpos.append(img_width + self.xspacing)
                elif i == len(files) - 1:
                    xpos.append(xpos[-1] + img_width)
                else:
                    xpos.append(xpos[-1] + img_width + self.xspacing)
                ypos.append(.5*(win_height - img_height))
            else:
                if not ypos:
                    ypos.append(img_height + self.xspacing)
                elif i == len(files) - 1:
                    ypos.append(ypos[-1] + img_height)
                else:
                    ypos.append(ypos[-1] + img_height + self.xspacing)
                xpos.append(.5*(win_width - img_width))

            pixbufs.append(pixbuf)

        # FIXME: separate layout rotation from image rotation?
        if not self.rotated:
            xpos.insert(0, 0)
            offset = .5*(win_width - xpos[-1] - xpos[0])
            xpos = [x + offset for x in xpos]
            xpos.pop()
        else:
            ypos.insert(0, 0)
            offset = .5*(win_height - ypos[-1] - ypos[0])
            ypos = [y + offset for y in ypos]
            ypos.pop()

        self.limits = [total_width*ratio, total_height*ratio]
        self.screen_size = [available_width, available_height]
        
        return zip(xpos, ypos, pixbufs)

    @assert_gui_thread
    def blit_image(self, pixbuf, dst_x, dst_y, clip_rect):
        pixbuf_area = gtk.gdk.Rectangle()
        pixbuf_area.width = pixbuf.get_width()
        pixbuf_area.height = pixbuf.get_height()

        xoff = int(dst_x)
        yoff = int(dst_y)

        clip_rect.x -= xoff
        clip_rect.y -= yoff
        area = pixbuf_area.intersect(clip_rect)

        if area.width > 0 and area.height > 0:
            self.window.draw_pixbuf(self.get_style().black_gc,
                                    pixbuf,
                                    area.x, area.y,
                                    int(area.x+xoff), int(area.y+yoff),
                                    area.width, area.height)
        return True


##############################################################################
## UI
##############################################################################

class CollectionUI(ImageView):
    
    def __init__(self, sources, filelist, ncolumns=1, rtl=False):
        self.sources = sources
        self.filelist = filelist
        
        self.cache = ImageCache(filelist)
        ImageView.__init__(self, self.cache)
        
        self.pos = 0
        self.rtl = rtl
        self.ncolumns = ncolumns
        self.preload_id = 0
        self.update_id = 0

        self.connect("map-event", self.__map_event)

    def next(self, count=1):
        last_pos = self.pos
        self.pos += count
        self.__limit_position()
        if last_pos == self.pos: return

        if not self.rtl:
            self.offset = [-1e9,-1e9]
        else:
            self.offset = [+1e9,-1e9]
        if self.rotated:
            self.offset.reverse()
        self.__schedule_update_position()

    def previous(self, count=1):
        last_pos = self.pos
        self.pos -= count
        self.__limit_position()
        if last_pos == self.pos: return

        if not self.rtl:
            self.offset = [+1e9,1e9]
        else:
            self.offset = [-1e9,1e9]
        if self.rotated:
            self.offset.reverse()
        self.__schedule_update_position()

    def next_screen(self, count=1):
        last_pos = self.pos
        self.pos += self.ncolumns*count
        self.__limit_position()
        if last_pos == self.pos: return

        if not self.rtl:
            self.offset = [-1e9,-1e9]
        else:
            self.offset = [+1e9,-1e9]
        if self.rotated:
            self.offset.reverse()
        self.__schedule_update_position()

    def previous_screen(self, count=1):
        last_pos = self.pos
        self.pos -= self.ncolumns*count
        self.__limit_position()
        if last_pos == self.pos: return

        if not self.rtl:
            self.offset = [+1e9,1e9]
        else:
            self.offset = [-1e9,1e9]
        if self.rotated:
            self.offset.reverse()
        self.__schedule_update_position()

    def adjust_zoom(self, step):
        scales = [1, 1.5, 2]

        try:
            j0 = scales.index(self.zoom_ratio)
        except ValueError:
            j0 = 0

        try:
            self.zoom_ratio = scales[j0 + step]
        except IndexError:
            if step > 0:
                self.zoom_ratio = scales[-1]
            else:
                self.zoom_ratio = scales[0]

        self.offset = [0, 0]
    
    def pan_around(self, dx, dy):
        """
        Pan the screen (dx, dy) half-screens.
        
        :Returns: True if position changed, false is screen limits hit.
        """
        xstep = self.screen_size[0]/2
        ystep = self.screen_size[1]/2

        last_offset = [self.offset[0],
                       self.offset[1]]
        
        self.offset[0] += dx*xstep
        self.offset[1] += dy*ystep

        self.normalize_offset()

        # did pan or hit edge?
        xmin = 3
        ymin = 3
        panned = (abs(self.offset[0]-last_offset[0]) > xmin or
                  abs(self.offset[1]-last_offset[1]) > ymin)
        return panned

    def goto(self, i):
        self.pos = i
        self.__limit_position()
        self.__schedule_update_position()

    def first(self):
        self.pos = 0
        self.__schedule_update_position()

    def last(self):
        self.pos = len(self.filelist) - self.ncolumns
        self.__schedule_update_position()

    def update_view(self):
        self.__limit_position()
        self.__schedule_update_position()

    def set_interpolation(self, interpolation):
        self.cache.set_interpolation(interpolation)

    def get_interpolation(self):
        return self.cache.interpolation

    @assert_gui_thread
    def __map_event(self, widget, event):
        self.__schedule_update_position()
        self.schedule_preload()
        return False
    
    def __limit_position(self):
        if self.pos > len(self.filelist) - self.ncolumns:
            self.pos = len(self.filelist) - self.ncolumns
        if self.pos < 0:
            self.pos = 0

    def preload(self, files, preload_id=0):
        if preload_id != self.preload_id:
            return # expired preload request
        preload_files = self.__get_preload_files()
        for i in range(0, len(preload_files), self.ncolumns):
            j = i + self.ncolumns
            if j >= len(preload_files):
                preload_files += [preload_files[-1]] * self.ncolumns
            ImageView.preload(self, preload_files[i:j])

    def schedule_preload(self, delay=750):
        self.preload_id += 1
        run_later_in_gui_thread(delay,
                                self.preload,
                                self.__get_preload_files(),
                                self.preload_id)

    def __update_position(self, update_id):
        if update_id != self.update_id: return
        
        files = self.__get_show_files()
        filenames = [ f for f in files ]
        filenames = [ os.path.join(os.path.basename(os.path.dirname(f)),
                                   os.path.basename(f))
                      for f in filenames ]
        self.text = u"%d / %d: %s" % (
            self.pos+1, len(self.filelist),
            unicode(', '.join(filenames), "latin-1"))
        self.set_files(files)

        self.schedule_preload()

    def __schedule_update_position(self, delay=10):
        self.update_id += 1
        run_later_in_gui_thread(delay, self.__update_position, self.update_id)

    def __get_show_files(self):
        endpos = self.pos + self.ncolumns
        if endpos > len(self.filelist):
            endpos = len(self.filelist)

        filelist = self.filelist[self.pos:endpos]

        if self.rtl:
            filelist.reverse()

        return filelist

    def __get_preload_files(self):
        files = []

        ranges = (range(0, 2*self.ncolumns+1, 1)
                  + range(-self.ncolumns,0,1))
        
        for i in ranges[:MAX_IMAGE_CACHE]:
            if 0 <= self.pos + i < len(self.filelist):
                files.append(self.filelist[self.pos + i])
        return files

class Config(dict):
    def load(self, filename):
        f = open(filename, "r")
        for line in f:
            try:
                key, value = line.split("\t", 1)
                self[key] = value
            except:
                pass
            

    def save(self, filename):
        f = open(filename, "w")
        for key, value in self.items():
            if key:
                f.write("%s\t%s\n" % (key, str(value)))

class Bookmarks(object):
    def __init__(self, sources, config):
        if not isinstance(sources, list):
            sources = [sources]

        self.config = config
        self.configkey = ':'.join(sources).replace("\t", "_")

        self.values = []

        try:
            if self.configkey in self.config:
                self.values = map(int, self.config[self.configkey].split("\t"))
        except:
            self.values = []
            
        while len(self.values) < 10:
            self.values.append(0)
        if len(self.values) > 10:
            self.values = self.values[0:10]

    def __setitem__(self, i, value):
        self.values[i] = int(value)
        self.config[self.configkey] = '\t'.join(map(str, self.values))

    def __getitem__(self, i):
        return self.values[i]

class PaiUI(object):

    def __init__(self, sources, filelist, config, rtl=False, ncolumns=2):
        self.config = config
        self.collection = CollectionUI(sources, filelist, ncolumns=ncolumns,
                                       rtl=rtl)
        self.bookmarks = Bookmarks(self.collection.sources, self.config)
        self.collection.goto(self.bookmarks[0])

        self.fullscreen = False

        self.collection.set_size_request(300, 200)
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.connect("destroy", self.destroy_event)
        self.window.connect("key-press-event", self.key_press_event)

        self.window.add_events(gtk.gdk.BUTTON_RELEASE_MASK
                               | gtk.gdk.BUTTON_PRESS_MASK)
        self.window.connect("button-release-event", self.button_release_event)
        self.window.add(self.collection)


    @assert_gui_thread
    def show(self):
        self.window.show_all()
        if self.fullscreen:
            self.window.fullscreen()


    def _do_left(self):
        if not self.collection.pan_around(-1, 0):
            if not self.collection.rotated:
                if not self.collection.rtl:
                    self.collection.previous()
                else:
                    self.collection.next()
            elif self.collection.zoom_ratio == 1:
                self.collection.next_screen(10)
        else:
            self.collection.update_view()

    def _do_right(self):
        if not self.collection.pan_around(1, 0):
            if not self.collection.rotated:
                if not self.collection.rtl:
                    self.collection.next()
                else:
                    self.collection.previous()
            elif self.collection.zoom_ratio == 1:
                self.collection.previous_screen(10)
        else:
            self.collection.update_view()

    def _do_up(self):
        if not self.collection.pan_around(0, -1):
            if self.collection.rotated:
                if not self.collection.rtl:
                    self.collection.previous()
                else:
                    self.collection.next()
            elif self.collection.zoom_ratio == 1:
                self.collection.previous_screen(10)
        else:
            self.collection.update_view()

    def _do_down(self):
        if not self.collection.pan_around(0, 1):
            if self.collection.rotated:
                if not self.collection.rtl:
                    self.collection.next()
                else:
                    self.collection.previous()
            elif self.collection.zoom_ratio == 1:
                self.collection.next_screen(10)
        else:
            self.collection.update_view()
        
    @assert_gui_thread
    def key_press_event(self, widget, event):
        if event.keyval in (gtk.keysyms.q, gtk.keysyms.Escape):
            self.close()

        elif event.keyval in (gtk.keysyms.space, gtk.keysyms.Return):
            self.collection.next_screen()

        elif event.keyval == gtk.keysyms.b:
            self.collection.previous_screen()

        elif event.keyval == gtk.keysyms.Home:
            self.collection.first()

        elif event.keyval == gtk.keysyms.End:
            self.collection.last()

        elif event.keyval == gtk.keysyms.Left:
            self._do_left()

        elif event.keyval == gtk.keysyms.Right:
            self._do_right()

        elif event.keyval == gtk.keysyms.Up:
            self._do_up()
            
        elif event.keyval == gtk.keysyms.Down:
            self._do_down()
            
        elif event.keyval == gtk.keysyms.Prior:
            self.collection.previous_screen(10)

        elif event.keyval == gtk.keysyms.Next:
            self.collection.next_screen(10)

        elif event.keyval == gtk.keysyms.r:
            self.collection.rtl = not self.collection.rtl
            self.collection.update_view()

        elif event.keyval == gtk.keysyms.d:
            if self.collection.ncolumns > 1:
                self.collection.ncolumns = 1
            else:
                self.collection.ncolumns = 2
            self.collection.update_view()

        elif event.keyval == gtk.keysyms.u:
            self.collection.unity_ratio = not self.collection.unity_ratio
            self.collection.update_view()

        elif event.keyval in (gtk.keysyms.plus, gtk.keysyms.F7):
            self.collection.adjust_zoom(1)
            self.collection.update_view()

        elif event.keyval in (gtk.keysyms.minus, gtk.keysyms.F8):
            self.collection.adjust_zoom(-1)
            self.collection.update_view()

        elif event.keyval in (gtk.keysyms.o, gtk.keysyms.F4):
            self.collection.rotated = not self.collection.rotated
            self.collection.update_view()

        elif event.keyval in (gtk.keysyms.f, gtk.keysyms.F6):
            if not self.fullscreen:
                self.window.fullscreen()
                self.fullscreen = True
            else:
                self.window.unfullscreen()
                self.fullscreen = False
        
        elif event.keyval == gtk.keysyms.i:
            if self.collection.get_interpolation() == gtk.gdk.INTERP_BILINEAR:
                self.collection.set_interpolation(gtk.gdk.INTERP_HYPER)
            elif self.collection.get_interpolation() == gtk.gdk.INTERP_HYPER:
                self.collection.set_interpolation(gtk.gdk.INTERP_NEAREST)
            else:
                self.collection.set_interpolation(gtk.gdk.INTERP_BILINEAR)
            self.collection.update_view()

    @assert_gui_thread
    def button_release_event(self, widget, event):
        x, y, w, h = self.collection.get_allocation()
        if event.x <= w/4:
            self._do_left()
        elif event.x >= w*3/4:
            self._do_right()
        elif event.y <= h/4:
            self._do_up()
        elif event.y >= h*3/4:
            self._do_down()
    
    @assert_gui_thread
    def close(self):
        self.bookmarks[0] = self.collection.pos
        gtk.main_quit()

    @assert_gui_thread
    def destroy_event(self, widget):
        self.close()

##############################################################################
## Progress dialog
##############################################################################

class ProgressDialog(object):
    """A dialog box that reports progress.

    >>> dlg = ProgressDialog()
    >>> dlg.set_progress(percentage=50, text="Foo")
    >>> dlg.close()
    """
    def __init__(self, title=""):
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_title(title)
        
        vbox = gtk.VBox(False, 2)
        vbox.set_border_width(10)
        self.window.add(vbox)
        vbox.show()

        self.label = gtk.Label()
        vbox.pack_start(self.label)
        self.label.show()
        
        self.bar = gtk.ProgressBar()
        vbox.pack_end(self.bar)
        self.bar.show()

        self.window.show()
            
        self.queue = Queue.Queue()
        self.__listener = threading.Thread(target=self.__listener)

        run_in_gui_thread(self.__listener.start)

    def __listener(self):
        while True:
            item = copy.copy(self.queue.get())
            if isinstance(item, str):
                run_in_gui_thread(self.bar.set_text, item)
                run_in_gui_thread(self.label.set_text, item)
                run_in_gui_thread(self.bar.pulse)
            elif item == None:
                run_in_gui_thread(self.window.destroy)
                return

    @assert_gui_thread
    def __getattr__(self, name):
        return getattr(self.bar, name)

    def close(self):
        self.queue.put(None)

##############################################################################
## Main
##############################################################################

def start(args, options, config):
    if not args:
        if HILDON:
            dummy_ui = gtk.Window()
            dlg = hildon.FileChooserDialog(
                dummy_ui, action=gtk.FILE_CHOOSER_ACTION_SELECT_FOLDER)
        else:
            dlg = gtk.FileChooserDialog(
                title="Open",
                action=gtk.FILE_CHOOSER_ACTION_SELECT_FOLDER,
                buttons=(gtk.STOCK_OPEN, gtk.RESPONSE_OK,
                         gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL)
                )
        dlg.set_local_only(True)
        dlg.set_select_multiple(True)
        
        result = dlg.run()
        if result != gtk.RESPONSE_OK:
            gtk.main_quit()
            sys.exit(0)
        args = dlg.get_filenames()
        dlg.destroy()
    
    sources = [os.path.realpath(p) for p in args if os.path.exists(p)]
    
    progress = ProgressDialog("Starting PAI...")
    def _load():
        filelist = FileList(sources, IMAGE_EXTENSIONS, progress.queue)
        run_in_gui_thread(_finished, filelist)
    
    @assert_gui_thread
    def _finished(filelist):
        progress.close()
        ui = PaiUI(sources, filelist, config, ncolumns=options.ncolumns,
                   rtl=options.rtl)
        ui.show()

    threading.Thread(target=_load).start()

def main():
    usage = ("%%prog [options] [images-or-something]...\n"
             "Picture archive inspector version %s" % __version__)
    parser = optparse.OptionParser(usage=usage)
    parser.add_option("-r", "--rtl", action="store_true", dest="rtl",
                      help="show images in right-to-left order", default=True)
    parser.add_option("-l", "--ltr", action="store_false", dest="rtl",
                      help="show images in left-to-right order")
    parser.add_option("-c", "--columns", type="int", dest="ncolumns",
                      help="show images in N columns", default=DEFAULT_COLUMNS)
    options, args = parser.parse_args()

    if options.ncolumns > 4 or options.ncolumns < 1:
        parser.error("invalid number of columns given")
        
    config_fn = "%s/.pairc" % os.environ["HOME"]
    config = Config()
    if os.path.exists(config_fn):
        config.load(config_fn)

    gtk.gdk.threads_init()

    gtk.gdk.threads_enter()
    run_in_gui_thread(start, args, options, config)
    gtk.main()
    gtk.gdk.threads_leave()
    
    config.save(config_fn)

    sys.exit(0)

if __name__ == "__main__": main()
