#!/usr/bin/env python
from __future__ import division

import sys, os, shutil, tempfile, UserDict
import pygtk
pygtk.require('2.0')
import gtk
import gobject
import gc


##############################################################################
## Image list / recursive archive unpack

class ExtensionMap(UserDict.UserDict):
    def __init__(self, dictionary=None):
        UserDict.UserDict.__init__(self)
        if dictionary:
            self.data = dictionary
        
    def has_key(self, string):
        for key in self.iterkeys():
            if string.endswith(key):
                return True
        return False

    def get(self, string, default=None):
        for key in self.iterkeys():
            if string.endswith(key):
                return self.data[key]
        return default

    def __contains__(self, string):
        return self.has_key(string)

    def __getitem__(self, string):
        for key in self.iterkeys():
            if string.endswith(key):
                return self.data[key]
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

def recursive_unpack(dirname, unpackers):
    """Unpack all archives in dirname and return a file list."""
    pathlist = [ dirname ]
    files = []

    while len(pathlist) > 0:
        path = pathlist[0]
        del pathlist[0]

        if os.path.isdir(path):
            pathlist += numeric_file_sort([os.path.join(path, i)
                                           for i in os.listdir(path)])
        elif os.path.isfile(path) and path in unpackers:
            unpacker = unpackers[path]
            root, ext = os.path.splitext(path)
            tmpname = tempfile.mktemp(ext, '', os.path.dirname(path))
            shutil.move(path, tmpname)
            os.mkdir(path)
            try:
                unpacker(tmpname, path)
                os.unlink(tmpname)
                pathlist.append(path)
            except:
                # unpack failed for some reason... do not do it then
                os.rmdir(path)
                shutil.move(tmpname, path)
        else:
            files.append(path)

    return files

def unpack_atool(archive, todir):
    exitcode = os.spawnlp(os.P_WAIT,
                          "aunpack",
                          "aunpack", "-X", todir, archive)
    if exitcode != 0:
        raise ValueError("Archive unpack failed")

class RecursiveFileList:
    zip_extension_map = ExtensionMap({
        '.zip': unpack_atool,
        '.tar': unpack_atool,
        '.tar.gz': unpack_atool,
        '.tar.bz2': unpack_atool,
        '.tbz': unpack_atool,
        '.tb2': unpack_atool, 
        '.tgz': unpack_atool,
        '.rar': unpack_atool,
        '.cbr': unpack_atool,
        })

    def __init__(self, filenames, extensionlist=None):
        if not isinstance(filenames, list):
            filenames = [filenames]
        
        self._cache_dir = tempfile.mkdtemp()

        # Link seed files
        for filename in filenames:
            os.symlink(filename,
                       os.path.join(self._cache_dir,
                                    os.path.basename(filename)))

        # Recursive unpack
        self._files = recursive_unpack(self._cache_dir,
                                       RecursiveFileList.zip_extension_map)

        # List
        if extensionlist:
            self._files = [i for i in self._files
                           if i[-4:].lower() in extensionlist]

    def close(self):
        if self._cache_dir:
            shutil.rmtree(self._cache_dir)
        self._cache_dir = None
        self._files = None

    def __del__(self):
        self.close()

    def filename(self, i):
        return self._files[i]

    def __getitem__(self, i):
        return self._files[i]

    def __len__(self):
        return len(self._files)

##############################################################################
## Image collection

class ImageCollection:
    def __init__(filenames):
        pass


##############################################################################
## UI

class ImageCache:
    def __init__(self, max_items=10):
        self.raw_pixbufs = {}
        self.scaled_pixbufs = {}
        self.filenames = []
        self.max_items = max_items
    
    def add(self, filename):
        if filename in self.filenames:
            return # noop
        
        # cleanup cache, if necessary
        if len(self.filenames) + 1 > self.max_items:
            del self.raw_pixbufs[self.filenames[0]]
            del self.scaled_pixbufs[self.filenames[0]]
            del self.filenames[0]
            gc.collect()

        # load image
        raw_pixbuf = gtk.gdk.pixbuf_new_from_file(filename)
        self.filenames.append(filename)
        self.raw_pixbufs[filename] = raw_pixbuf

    def get(self, filename):
        self.add(filename)
        return self.raw_pixbufs[filename]

    def get_scaled(self, filename, width, height):
        self.add(filename)
        try:
            pixbuf = self.scaled_pixbufs[filename]
            if pixbuf.get_width() != width or pixbuf.get_height() != height:
                raise KeyError(filename)
            return pixbuf
        except KeyError:
            pixbuf = self.raw_pixbufs[filename].scale_simple(
                width, height, gtk.gdk.INTERP_BILINEAR)
            self.scaled_pixbufs[filename] = pixbuf
            gc.collect()
            return pixbuf

class ScalingImage(gtk.DrawingArea):
    def __init__(self, cache, filename):
        gtk.DrawingArea.__init__(self)

        self.cache = cache
        self.filename = filename



class ImageView(gtk.DrawingArea):
    __gsignals__ = {
        'expose-event': 'override',
        }
    
    def __init__(self, cache, xspacing=0, direction=1):
        gtk.Container.__init__(self)
        self.direction = direction
        self.xspacing = xspacing
        self.cache = cache

        self.filenames = []

        style = self.get_style().copy()
        style.bg[gtk.STATE_NORMAL] = gtk.gdk.Color(0, 0, 0)
        self.set_style(style)

    def set_files(self, filenames):
        if not isinstance(filenames, list):
            filenames = [filenames]
        self.filenames = filenames
        self.queue_resize()

    def do_expose_event(self, event):
        x, y, width, height = self.get_allocation()

        if not self.cache or not self.filenames:
            return False

        to_show = self.__get_files_to_show(self.filenames, width, height)

        for xpos, ypos, pixbuf in to_show:
            self.blit_image(pixbuf, xpos, ypos, event.area)

    def preload(self, filenames):
        x, y, width, height = self.get_allocation()
        
        if not isinstance(filenames, list):
            filenames = [filenames]

        self.__get_files_to_show(filenames, width, height)

    def __get_files_to_show(self, files, win_width, win_height):
        xpos = []
        ypos = []
        pixbufs = []

        available_width = win_width - self.xspacing*(len(files) - 1)
        available_height = win_height

        for i in range(len(files)):
            raw_pixbuf = self.cache.get(files[i])
            img_width = raw_pixbuf.get_width()
            img_height = raw_pixbuf.get_height()
            ratio = min(available_width / len(files) / img_width,
                        available_height / img_height)
            img_width *= ratio
            img_height *= ratio
            
            pixbuf = self.cache.get_scaled(files[i],
                                           int(img_width), int(img_height))
            if not xpos:
                xpos.append(img_width + self.xspacing)
            elif i == len(files) - 1:
                xpos.append(xpos[-1] + img_width)
            else:
                xpos.append(xpos[-1] + img_width + self.xspacing)
            ypos.append(.5*(win_height - img_height))
            pixbufs.append(pixbuf)

        xpos.insert(0, 0)
        offset = .5*(win_width - xpos[-1] - xpos[0])
        xpos = [x + offset for x in xpos]
        xpos.pop()
        
        return zip(xpos, ypos, pixbufs)

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


class PaiUI:
    def __init__(self):
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)

        self.cache = ImageCache()

        self.area = ImageView(self.cache, xspacing=0)
        self.area.set_files(["../test/006.jpg", "../test/007.jpg", "../test/008.jpg"])
        self.area.set_size_request(300, 200)

        self.window.add(self.area)

        style = self.window.get_style().copy()
        style.bg[gtk.STATE_NORMAL] = gtk.gdk.Color(0, 0, 0)
        self.window.set_style(style)

        self.window.show_all()
        self.window.connect("destroy", self.destroy_event)


    def destroy_event(self, widget):
        gtk.main_quit()

    def main(self):
        gtk.main()

if __name__ == "__main__":
    ui = PaiUI()
    ui.main()
    
