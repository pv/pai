#!/usr/bin/env python

import sys, os, shutil, tempfile, UserDict
import pygtk
pygtk.require('2.0')
import gtk

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

