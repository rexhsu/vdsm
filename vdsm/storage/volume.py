#
# Copyright 2009-2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import os.path
import logging
from contextlib import contextmanager

import image

from vdsm import qemuimg
from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import fileUtils
from vdsm.storage import misc
from vdsm.storage.misc import deprecated
from vdsm.storage.threadlocal import vars
from vdsm.storage.volumemetadata import VolumeMetadata

import sd
from sdc import sdCache
import task
import resourceManager as rm
rmanager = rm.ResourceManager.getInstance()


DOMAIN_MNT_POINT = 'mnt'

log = logging.getLogger('Storage.Volume')


def getBackingVolumePath(imgUUID, volUUID):
    # We used to return a relative path ../<imgUUID>/<volUUID> but this caused
    # unnecessary growth in the backing chain paths with repeated live merge
    # operations (see https://bugzilla.redhat.com/show_bug.cgi?id=1333627).
    # Since all volumes of an image are in the same directory (including cloned
    # templates which have a hard link on file domains and a symlink on block
    # domains) we do not need to use the image dir reference anymore.
    return volUUID


class VmVolumeInfo(object):
    TYPE_PATH = "path"
    TYPE_NETWORK = "network"


class VolumeManifest(object):
    log = logging.getLogger('Storage.VolumeManifest')

    def __init__(self, repoPath, sdUUID, imgUUID, volUUID):
        self.repoPath = repoPath
        self.sdUUID = sdUUID
        self.imgUUID = imgUUID
        self.volUUID = volUUID
        self._volumePath = None
        self._imagePath = None
        self.voltype = None

        if not imgUUID or imgUUID == sc.BLANK_UUID:
            raise se.InvalidParameterException("imgUUID", imgUUID)
        if not volUUID or volUUID == sc.BLANK_UUID:
            raise se.InvalidParameterException("volUUID", volUUID)
        self.validate()

    @property
    def imagePath(self):
        if self._imagePath is None:
            self.validateImagePath()
        return self._imagePath

    @property
    def volumePath(self):
        if self._volumePath is None:
            self.validateVolumePath()
        return self._volumePath

    @volumePath.setter
    def volumePath(self, value):
        self._volumePath = value

    def validate(self):
        """
        Validate that the volume can be accessed
        """
        self.validateImagePath()
        self.validateVolumePath()

    def getMetaParam(self, key):
        """
        Get a value of a specific key
        """
        meta = self.getMetadata()
        try:
            return meta[key]
        except KeyError:
            raise se.MetaDataKeyNotFoundError(str(meta) + ":" + str(key))

    def getVolumePath(self):
        """
        Get the path of the volume file/link
        """
        if not self._volumePath:
            raise se.VolumeAccessError(self.volUUID)
        return self._volumePath

    def getVolType(self):
        if not self.voltype:
            self.voltype = self.getMetaParam(sc.VOLTYPE)
        return self.voltype

    def isLeaf(self):
        return self.getVolType() == sc.type2name(sc.LEAF_VOL)

    def isShared(self):
        return self.getVolType() == sc.type2name(sc.SHARED_VOL)

    def getDescription(self):
        """
        Return volume description
        """
        return self.getMetaParam(sc.DESCRIPTION)

    def getLegality(self):
        """
        Return volume legality
        """
        try:
            legality = self.getMetaParam(sc.LEGALITY)
            return legality
        except se.MetaDataKeyNotFoundError:
            return sc.LEGAL_VOL

    def isLegal(self):
        try:
            legality = self.getMetaParam(sc.LEGALITY)
            return legality != sc.ILLEGAL_VOL
        except se.MetaDataKeyNotFoundError:
            return True

    def isFake(self):
        try:
            legality = self.getMetaParam(sc.LEGALITY)
            return legality == sc.FAKE_VOL
        except se.MetaDataKeyNotFoundError:
            return False

    def getSize(self):
        size = int(self.getMetaParam(sc.SIZE))
        if size < 1:  # Size stored in the metadata is not valid
            raise se.MetaDataValidationError()
        return size

    def getFormat(self):
        return sc.name2type(self.getMetaParam(sc.FORMAT))

    def getType(self):
        return sc.name2type(self.getMetaParam(sc.TYPE))

    def getDiskType(self):
        return self.getMetaParam(sc.DISKTYPE)

    def isInternal(self):
        return self.getVolType() == sc.type2name(sc.INTERNAL_VOL)

    def isSparse(self):
        return self.getType() == sc.SPARSE_VOL

    def metadata2info(self, meta):
        return {
            "uuid": self.volUUID,
            "type": meta.get(sc.TYPE, ""),
            "format": meta.get(sc.FORMAT, ""),
            "disktype": meta.get(sc.DISKTYPE, ""),
            "voltype": meta.get(sc.VOLTYPE, ""),
            "size": int(meta.get(sc.SIZE, "0")),
            "parent": self.getParent(),
            "description": meta.get(sc.DESCRIPTION, ""),
            "pool": meta.get(sc.POOL, ""),
            "domain": meta.get(sc.DOMAIN, ""),
            "image": self.getImage(),
            "ctime": meta.get(sc.CTIME, ""),
            "mtime": "0",
            "legality": meta.get(sc.LEGALITY, ""),
            }

    def getInfo(self):
        """
        Get volume info
        """
        self.log.info("Info request: sdUUID=%s imgUUID=%s volUUID = %s ",
                      self.sdUUID, self.imgUUID, self.volUUID)
        info = {}
        try:
            meta = self.getMetadata()
            info = self.metadata2info(meta)
            info["capacity"] = str(int(info["size"]) * sc.BLOCK_SIZE)
            del info["size"]
            # Get the image actual size on disk
            vsize = self.getVolumeSize(bs=1)
            avsize = self.getVolumeTrueSize(bs=1)
            info['apparentsize'] = str(vsize)
            info['truesize'] = str(avsize)
            info['status'] = "OK"
        except se.StorageException as e:
            self.log.debug("exception: %s:%s" % (str(e.message), str(e.value)))
            info['apparentsize'] = "0"
            info['truesize'] = "0"
            info['status'] = "INVALID"

        # Both engine and dumpStorageTable don't use this option so
        # only keeping it to not break existing scripts that look for the key
        info['children'] = []

        # If image was set to illegal, mark the status same
        # (because of VDC constraints)
        if info.get('legality', None) == sc.ILLEGAL_VOL:
            info['status'] = sc.ILLEGAL_VOL
        self.log.info("%s/%s/%s info is %s",
                      self.sdUUID, self.imgUUID, self.volUUID, str(info))
        return info

    def getVolumeParams(self, bs=sc.BLOCK_SIZE):
        volParams = {}
        volParams['volUUID'] = self.volUUID
        volParams['imgUUID'] = self.getImage()
        volParams['path'] = self.getVolumePath()
        volParams['disktype'] = self.getDiskType()
        volParams['prealloc'] = self.getType()
        volParams['volFormat'] = self.getFormat()
        # TODO: getSize returns size in 512b multiples, should move all sizes
        # to byte multiples everywhere to avoid conversion errors and change
        # only at the end
        volParams['size'] = self.getSize()
        volParams['apparentsize'] = self.getVolumeSize(bs=bs)
        volParams['truesize'] = self.getVolumeTrueSize(bs=bs)
        volParams['parent'] = self.getParent()
        volParams['descr'] = self.getDescription()
        volParams['legality'] = self.getLegality()
        return volParams

    def getVmVolumeInfo(self):
        """
        Get volume path/info as dict.
        Derived classes can use this if they want to represent the
        volume to the VM in a different way than the standard 'path' way.
        """
        # By default, send path
        return {'volType': VmVolumeInfo.TYPE_PATH,
                'path': self.getVolumePath()}

    def setMetaParam(self, key, value):
        """
        Set a value of a specific key
        """
        meta = self.getMetadata()
        try:
            meta[str(key)] = str(value)
            self.setMetadata(meta)
        except Exception:
            self.log.error("Volume.setMetaParam: %s: %s=%s" %
                           (self.volUUID, key, value))
            raise

    @classmethod
    def formatMetadata(cls, meta):
        """
        Format metadata string in storage format.

        Raises MetadataOverflowError if formatted metadata is too long.
        """
        # TODO: Adopt VolumeMetadata.storage_format()
        lines = ["%s=%s\n" % (key.strip(), str(value).strip())
                 for key, value in meta.iteritems()]
        lines.append("EOF\n")
        data = "".join(lines)
        if len(data) > sc.METADATA_SIZE:
            raise se.MetadataOverflowError(data)
        return data

    @deprecated  # valid for domain version < 3
    def setrw(self, rw):
        # Since domain version 3 (V3) VDSM is not changing the internal volumes
        # permissions to read-only because it would interfere with the live
        # snapshots and the live merge processes. E.g.: during a live snapshot
        # if the VM is running on the SPM it would lose the ability to write to
        # the current volume.
        # However to avoid lvm MDA corruption we still need to set the volume
        # as read-only on domain version 2. The corruption is triggered on the
        # HSMs that are using the resource manager to prepare the volume chain.
        if int(sdCache.produce(self.sdUUID).getVersion()) < 3:
            self._setrw(rw=rw)

    def setLeaf(self):
        self.setMetaParam(sc.VOLTYPE, sc.type2name(sc.LEAF_VOL))
        self.voltype = sc.type2name(sc.LEAF_VOL)
        self.setrw(rw=True)
        return self.voltype

    def setInternal(self):
        self.setMetaParam(sc.VOLTYPE, sc.type2name(sc.INTERNAL_VOL))
        self.voltype = sc.type2name(sc.INTERNAL_VOL)
        self.setrw(rw=False)
        return self.voltype

    def recheckIfLeaf(self):
        """
        Recheck if I am a leaf.
        """

        if self.isShared():
            return False

        type = self.getVolType()
        childrenNum = len(self.getChildren())

        if childrenNum == 0 and type != sc.LEAF_VOL:
            self.setLeaf()
        elif childrenNum > 0 and type != sc.INTERNAL_VOL:
            self.setInternal()

        return self.isLeaf()

    def setDescription(self, descr):
        """
        Set Volume Description
            'descr' - volume description
        """
        descr = VolumeMetadata.validate_description(descr)
        self.log.info("volUUID = %s descr = %s ", self.volUUID, descr)
        self.setMetaParam(sc.DESCRIPTION, descr)

    def setLegality(self, legality):
        """
        Set Volume Legality
            'legality' - volume legality
        """
        self.log.info("sdUUID=%s imgUUID=%s volUUID = %s legality = %s ",
                      self.sdUUID, self.imgUUID, self.volUUID, legality)
        self.setMetaParam(sc.LEGALITY, legality)

    def setDomain(self, sdUUID):
        self.setMetaParam(sc.DOMAIN, sdUUID)
        self.sdUUID = sdUUID
        return self.sdUUID

    def setShared(self):
        self.setMetaParam(sc.VOLTYPE, sc.type2name(sc.SHARED_VOL))
        self.voltype = sc.type2name(sc.SHARED_VOL)
        self.setrw(rw=False)
        return self.voltype

    def setSize(self, size):
        self.setMetaParam(sc.SIZE, size)

    def updateInvalidatedSize(self):
        # During some complex flows the volume size might have been marked as
        # invalidated (e.g. during a transaction). Here we are checking
        # NOTE: the prerequisite to run this is that the volume is accessible
        # (e.g. lv active) and not in use by another process (e.g. dd, qemu).
        # Going directly to the metadata parameter as we should skip the size
        # validation in getSize.
        if int(self.getMetaParam(sc.SIZE)) < 1:
            volInfo = qemuimg.info(
                self.getVolumePath(), sc.fmt2str(self.getFormat()))
            # qemu/qemu-img rounds down
            self.setSize(volInfo['virtualsize'] / sc.BLOCK_SIZE)

    def setType(self, prealloc):
        self.setMetaParam(sc.TYPE, sc.type2name(prealloc))

    def setFormat(self, volFormat):
        self.setMetaParam(sc.FORMAT, sc.type2name(volFormat))

    def validateDelete(self):
        """
        Validate volume before deleting
        """
        try:
            if self.isShared():
                raise se.CannotDeleteSharedVolume("img %s vol %s" %
                                                  (self.imgUUID, self.volUUID))
        except se.MetaDataKeyNotFoundError as e:
            # In case of metadata key error, we have corrupted
            # volume (One of metadata corruptions may be
            # previous volume deletion failure).
            # So, there is no reasons to avoid its deletion
            self.log.warn("Volume %s metadata error (%s)",
                          self.volUUID, str(e))
        if self.getChildren():
            raise se.VolumeImageHasChildren(self)

    @classmethod
    def createMetadata(cls, metaId, meta):
        cls._putMetadata(metaId, meta)

    @classmethod
    def newMetadata(cls, metaId, sdUUID, imgUUID, puuid, size, format, type,
                    voltype, disktype, desc="", legality=sc.ILLEGAL_VOL):
        meta = VolumeMetadata(sdUUID, imgUUID, puuid, size, format, type,
                              voltype, disktype, desc, legality)
        cls.createMetadata(metaId, meta.legacy_info())
        return meta

    def refreshVolume(self):
        pass

    def _shareLease(self, dstImgPath):
        """
        Internal utility method used during the share process and by the
        domain V3 upgrade.
        """
        pass  # Do not remove this method or the V3 upgrade will fail.

    def getParentVolume(self):
        """
        Return parent VolumeManifest object
        """
        puuid = self.getParent()
        if puuid and puuid != sc.BLANK_UUID:
            sd_manifest = sdCache.produce(self.sdUUID).manifest
            return sd_manifest.produceVolume(self.imgUUID, puuid)
        return None

    def prepare(self, rw=True, justme=False,
                chainrw=False, setrw=False, force=False):
        """
        Prepare volume for use by consumer.
        If justme is false, the entire COW chain is prepared.
        Note: setrw arg may be used only by SPM flows.
        """
        self.log.info("Volume: preparing volume %s/%s",
                      self.sdUUID, self.volUUID)

        if not force:
            # Cannot prepare ILLEGAL volume
            if not self.isLegal():
                raise se.prepareIllegalVolumeError(self.volUUID)

            if rw and self.isShared():
                if chainrw:
                    rw = False      # Shared cannot be set RW
                else:
                    raise se.SharedVolumeNonWritable(self)

            if (not chainrw and rw and self.isInternal() and setrw and
                    not self.recheckIfLeaf()):
                raise se.InternalVolumeNonWritable(self)

        self.llPrepare(rw=rw, setrw=setrw)
        self.updateInvalidatedSize()

        try:
            if justme:
                return True
            pvol = self.getParentVolume()
            if pvol:
                pvol.prepare(rw=chainrw, justme=False,
                             chainrw=chainrw, setrw=setrw)
        except Exception:
            self.log.error("Unexpected error", exc_info=True)
            self.teardown(self.sdUUID, self.volUUID)
            raise

        return True

    @classmethod
    def teardown(cls, sdUUID, volUUID, justme=False):
        """
        Teardown volume.
        If justme is false, the entire COW chain is teared down.
        """
        pass


class Volume(object):
    log = logging.getLogger('Storage.Volume')
    manifestClass = VolumeManifest

    def __init__(self, manifest):
        self._manifest = manifest

    @property
    def sdUUID(self):
        return self._manifest.sdUUID

    @property
    def imgUUID(self):
        return self._manifest.imgUUID

    @property
    def volUUID(self):
        return self._manifest.volUUID

    @property
    def repoPath(self):
        return self._manifest.repoPath

    @property
    def volumePath(self):
        return self._manifest.volumePath

    @property
    def imagePath(self):
        return self._manifest.imagePath

    @property
    def voltype(self):
        return self._manifest.voltype

    def getMetadataId(self):
        return self._manifest.getMetadataId()

    def getMetadata(self, metaId=None):
        """
        Get Meta data array of key,values lines
        """
        return self._manifest.getMetadata(metaId)

    def getParent(self):
        """
        Return parent volume UUID
        """
        return self._manifest.getParent()

    def getChildren(self):
        """ Return children volume UUIDs.

        Children can be found in any image of the volume SD.
        """
        return self._manifest.getChildren()

    def getImage(self):
        return self._manifest.getImage()

    @deprecated  # valid only for domain version < 3, see volume.setrw
    def _setrw(self, rw):
        """
        Set the read/write permission on the volume (deprecated)
        """
        self._manifest._setrw(rw)

    def _share(self, dstImgPath):
        return self._manifest._share(dstImgPath)

    @classmethod
    def formatMetadata(cls, meta):
        return cls.manifestClass.formatMetadata(meta)

    @classmethod
    def _putMetadata(cls, metaId, meta):
        cls.manifestClass._putMetadata(metaId, meta)

    def setMetadata(self, meta, metaId=None):
        return self._manifest.setMetadata(meta, metaId)

    @classmethod
    def _getModuleAndClass(cls):
        clsName = cls.__name__
        clsModule = cls.__module__.split(".").pop()
        return clsModule, clsName

    def validate(self):
        """
        Validate that the volume can be accessed
        """
        self._manifest.validateImagePath()
        self._manifest.validateVolumePath()

    def __str__(self):
        return str(self.volUUID)

    # Even if it's not in use anymore we cannot remove this method because
    # we might have persisted recovery on storage calling it.
    # TODO: remove this in the next version.
    @classmethod
    def killProcRollback(cls, taskObj, pid, ctime):
        cls.log.info('ignoring killProcRollback request for pid %s and '
                     'ctime %s', pid, ctime)

    @classmethod
    def rebaseVolumeRollback(cls, taskObj, sdUUID, srcImg,
                             srcVol, dstFormat, srcParent, unsafe):
        """
        Rebase volume rollback
        """
        cls.log.info('rebase volume rollback (sdUUID=%s srcImg=%s srcVol=%s '
                     'dstFormat=%s srcParent=%s)', sdUUID, srcImg, srcVol,
                     dstFormat, srcParent)

        imageResourcesNamespace = sd.getNamespace(sdUUID, sc.IMAGE_NAMESPACE)

        with rmanager.acquireResource(imageResourcesNamespace,
                                      srcImg, rm.LockType.exclusive):
            vol = sdCache.produce(sdUUID).produceVolume(srcImg, srcVol)
            vol.prepare(rw=True, chainrw=True, setrw=True)

            volumePath = vol.getVolumePath()
            backingVolPath = getBackingVolumePath(srcImg, srcParent)

            try:
                qemuimg.rebase(volumePath, backingVolPath,
                               sc.fmt2str(vol.getFormat()),
                               sc.fmt2str(int(dstFormat)),
                               misc.parseBool(unsafe), vars.task.aborting)
                vol.setParent(srcParent)
                vol.recheckIfLeaf()
            except qemuimg.QImgError:
                cls.log.exception('cannot rollback rebase for volume %s on '
                                  '%s', volumePath, backingVolPath)
                raise se.MergeVolumeRollbackError(srcVol)
            finally:
                vol.teardown(sdUUID, srcVol)

    def rebase(self, backingVol, backingVolPath, backingFormat, unsafe,
               rollback):
        """
        Rebase volume on top of new backing volume
        """
        if rollback:
            pvol = self.getParentVolume()
            if not pvol:
                self.log.warn("Can't rebase volume %s, parent missing",
                              self.volUUID)
                return

            name = "Merge volume: " + self.volUUID
            vars.task.pushRecovery(
                task.Recovery(name, "volume", "Volume",
                              "rebaseVolumeRollback",
                              [self.sdUUID, self.getImage(),
                                  self.volUUID, str(pvol.getFormat()),
                                  pvol.volUUID, str(True)]))

        volumePath = self.getVolumePath()

        try:
            qemuimg.rebase(volumePath, backingVolPath,
                           sc.fmt2str(self.getFormat()),
                           sc.fmt2str(backingFormat), unsafe,
                           vars.task.aborting)
        except qemuimg.QImgError:
            self.log.exception('cannot rebase volume %s on %s', volumePath,
                               backingVolPath)
            raise se.MergeSnapshotsError(self.volUUID)

        self.setParent(backingVol)
        self.recheckIfLeaf()

    def clone(self, dstPath, volFormat):
        """
        Clone self volume to the specified dst_image_dir/dst_volUUID
        """
        wasleaf = False
        taskName = "parent volume rollback: " + self.volUUID
        vars.task.pushRecovery(
            task.Recovery(taskName, "volume", "Volume",
                          "parentVolumeRollback",
                          [self.sdUUID, self.imgUUID, self.volUUID]))
        if self.isLeaf():
            wasleaf = True
            self.setInternal()
        try:
            self.prepare(rw=False)
            self.log.debug('cloning volume %s to %s', self.volumePath,
                           dstPath)
            parent = getBackingVolumePath(self.imgUUID, self.volUUID)
            qemuimg.create(dstPath, backing=parent,
                           format=sc.fmt2str(volFormat),
                           backingFormat=sc.fmt2str(self.getFormat()))
            self.teardown(self.sdUUID, self.volUUID)
        except Exception as e:
            self.log.exception('cannot clone image %s volume %s to %s',
                               self.imgUUID, self.volUUID, dstPath)
            # FIXME: might race with other clones
            if wasleaf:
                self.setLeaf()
            self.teardown(self.sdUUID, self.volUUID)
            raise se.CannotCloneVolume(self.volumePath, dstPath, str(e))

    def _shareLease(self, dstImgPath):
        self._manifest._shareLease(dstImgPath)

    def share(self, dstImgPath):
        """
        Share this volume to dstImgPath
        """
        self.log.debug("Share volume %s to %s", self.volUUID, dstImgPath)

        if not self.isShared():
            raise se.VolumeNonShareable(self)

        if os.path.basename(dstImgPath) == os.path.basename(self.imagePath):
            raise se.VolumeOwnershipError(self)

        dstPath = os.path.join(dstImgPath, self.volUUID)
        clsModule, clsName = self._getModuleAndClass()

        try:
            vars.task.pushRecovery(
                task.Recovery("Share volume rollback: %s" % dstPath, clsModule,
                              clsName, "shareVolumeRollback", [dstPath])
            )

            self._share(dstImgPath)

        except Exception as e:
            raise se.CannotShareVolume(self.getVolumePath(), dstPath, str(e))

    def refreshVolume(self):
        return self._manifest.refreshVolume()

    @classmethod
    def parentVolumeRollback(cls, taskObj, sdUUID, pimgUUID, pvolUUID):
        cls.log.info("parentVolumeRollback: sdUUID=%s pimgUUID=%s"
                     " pvolUUID=%s" % (sdUUID, pimgUUID, pvolUUID))
        if pvolUUID != sc.BLANK_UUID and pimgUUID != sc.BLANK_UUID:
            pvol = sdCache.produce(sdUUID).produceVolume(pimgUUID,
                                                         pvolUUID)
            pvol.prepare()
            try:
                pvol.recheckIfLeaf()
            except Exception:
                cls.log.error("Unexpected error", exc_info=True)
            finally:
                pvol.teardown(sdUUID, pvolUUID)

    @classmethod
    def startCreateVolumeRollback(cls, taskObj, sdUUID, imgUUID, volUUID):
        cls.log.info("startCreateVolumeRollback: sdUUID=%s imgUUID=%s "
                     "volUUID=%s " % (sdUUID, imgUUID, volUUID))
        # This rollback doesn't actually do anything.
        # In general the createVolume rollbacks are a list of small rollbacks
        # that are replaced by the one major rollback at the end of the task.
        # This rollback is a simple marker that must be the first rollback
        # in the list of createVolume rollbacks.
        # We need it in cases when createVolume is part of a composite task and
        # not a task by itself. In such cases when we will replace the list of
        # small rollbacks with the major one, we want to be able remove only
        # the relevant rollbacks from the rollback list.
        pass

    @classmethod
    def createVolumeRollback(cls, taskObj, repoPath,
                             sdUUID, imgUUID, volUUID, imageDir):
        cls.log.info("createVolumeRollback: repoPath=%s sdUUID=%s imgUUID=%s "
                     "volUUID=%s imageDir=%s" %
                     (repoPath, sdUUID, imgUUID, volUUID, imageDir))
        vol = sdCache.produce(sdUUID).produceVolume(imgUUID, volUUID)
        pvol = vol.getParentVolume()
        # Remove volume
        vol.delete(postZero=False, force=True)
        if len(cls.getImageVolumes(repoPath, sdUUID, imgUUID)):
            # Don't remove the image folder itself
            return

        if not pvol or pvol.isShared():
            # Remove image folder with all leftovers
            if os.path.exists(imageDir):
                fileUtils.cleanupdir(imageDir)

    @classmethod
    def create(cls, repoPath, sdUUID, imgUUID, size, volFormat, preallocate,
               diskType, volUUID, desc, srcImgUUID, srcVolUUID,
               initialSize=None):
        """
        Create a new volume with given size or snapshot
            'size' - in sectors
            'volFormat' - volume format COW / RAW
            'preallocate' - Preallocate / Sparse
            'diskType' - enum (API.Image.DiskTypes)
            'srcImgUUID' - source image UUID
            'srcVolUUID' - source volume UUID
            'initialSize' - initial volume size in sectors,
                            in case of thin provisioning
        """
        dom = sdCache.produce(sdUUID)
        dom.validateCreateVolumeParams(volFormat, srcVolUUID,
                                       preallocate=preallocate)

        imgPath = image.Image(repoPath).create(sdUUID, imgUUID)

        volPath = os.path.join(imgPath, volUUID)
        volParent = None
        volType = sc.type2name(sc.LEAF_VOL)

        # Get the specific class name and class module to be used in the
        # Recovery tasks.
        clsModule, clsName = cls._getModuleAndClass()

        try:
            if srcVolUUID != sc.BLANK_UUID:
                # When the srcImgUUID isn't specified we assume it's the same
                # as the imgUUID
                if srcImgUUID == sc.BLANK_UUID:
                    srcImgUUID = imgUUID

                volParent = cls(repoPath, sdUUID, srcImgUUID, srcVolUUID)

                if not volParent.isLegal():
                    raise se.createIllegalVolumeSnapshotError(
                        volParent.volUUID)

                if imgUUID != srcImgUUID:
                    volParent.share(imgPath)
                    volParent = cls(repoPath, sdUUID, imgUUID, srcVolUUID)

                # Override the size with the size of the parent
                size = volParent.getSize()

        except se.StorageException:
            cls.log.error("Unexpected error", exc_info=True)
            raise
        except Exception as e:
            cls.log.error("Unexpected error", exc_info=True)
            raise se.VolumeCannotGetParent(
                "Couldn't get parent %s for volume %s: %s" %
                (srcVolUUID, volUUID, e))

        try:
            cls.log.info("Creating volume %s", volUUID)

            # Rollback sentinel to mark the start of the task
            vars.task.pushRecovery(
                task.Recovery(task.ROLLBACK_SENTINEL, clsModule, clsName,
                              "startCreateVolumeRollback",
                              [sdUUID, imgUUID, volUUID])
            )

            # Create volume rollback
            vars.task.pushRecovery(
                task.Recovery("Halfbaked volume rollback", clsModule, clsName,
                              "halfbakedVolumeRollback",
                              [sdUUID, volUUID, volPath])
            )

            # Specific volume creation (block, file, etc...)
            try:
                metaId = cls._create(dom, imgUUID, volUUID, size, volFormat,
                                     preallocate, volParent, srcImgUUID,
                                     srcVolUUID, volPath,
                                     initialSize=initialSize)
            except (se.VolumeAlreadyExists, se.CannotCreateLogicalVolume,
                    se.VolumeCreationError, se.InvalidParameterException) as e:
                cls.log.error("Failed to create volume %s: %s", volPath, e)
                vars.task.popRecovery()
                raise
            # When the volume format is raw what the guest sees is the apparent
            # size of the file/device therefore if the requested size doesn't
            # match the apparent size (eg: physical extent granularity in LVM)
            # we need to update the size value so that the metadata reflects
            # the correct state.
            if volFormat == sc.RAW_FORMAT:
                apparentSize = int(dom.getVSize(imgUUID, volUUID) /
                                   sc.BLOCK_SIZE)
                if apparentSize < size:
                    cls.log.error("The volume %s apparent size %s is smaller "
                                  "than the requested size %s",
                                  volUUID, apparentSize, size)
                    raise se.VolumeCreationError()
                if apparentSize > size:
                    cls.log.info("The requested size for volume %s doesn't "
                                 "match the granularity on domain %s, "
                                 "updating the volume size from %s to %s",
                                 volUUID, sdUUID, size, apparentSize)
                    size = apparentSize

            vars.task.pushRecovery(
                task.Recovery("Create volume metadata rollback", clsModule,
                              clsName, "createVolumeMetadataRollback",
                              map(str, metaId))
            )

            cls.newMetadata(metaId, sdUUID, imgUUID, srcVolUUID, size,
                            sc.type2name(volFormat), sc.type2name(preallocate),
                            volType, diskType, desc, sc.LEGAL_VOL)

            if dom.hasVolumeLeases():
                cls.newVolumeLease(metaId, sdUUID, volUUID)

        except se.StorageException:
            cls.log.error("Unexpected error", exc_info=True)
            raise
        except Exception as e:
            cls.log.error("Unexpected error", exc_info=True)
            raise se.VolumeCreationError("Volume creation %s failed: %s" %
                                         (volUUID, e))

        # Remove the rollback for the halfbaked volume
        vars.task.replaceRecoveries(
            task.Recovery("Create volume rollback", clsModule, clsName,
                          "createVolumeRollback",
                          [repoPath, sdUUID, imgUUID, volUUID, imgPath])
        )

        return volUUID

    def validateDelete(self):
        self._manifest.validateDelete()

    def extend(self, newsize):
        """
        Extend the apparent size of logical volume (thin provisioning)
        """
        pass

    def reduce(self, newsize):
        """
        reduce a logical volume
        """
        pass

    def syncMetadata(self):
        volFormat = self.getFormat()
        if volFormat != sc.RAW_FORMAT:
            self.log.error("impossible to update metadata for volume %s ",
                           "its format is not RAW", self.volUUID)
            return

        newVolSize = self.getVolumeSize()
        oldVolSize = self.getSize()

        if oldVolSize == newVolSize:
            self.log.debug("size metadata %s is up to date for volume %s",
                           oldVolSize, self.volUUID)
        else:
            self.log.debug("updating metadata for volume %s changing the "
                           "size %s to %s", self.volUUID, oldVolSize,
                           newVolSize)
            self.setSize(newVolSize)

    @classmethod
    def extendSizeFinalize(cls, taskObj, sdUUID, imgUUID, volUUID):
        cls.log.debug("finalizing size extension for volume %s on domain "
                      "%s", volUUID, sdUUID)
        # The rollback consists in just updating the metadata to be
        # consistent with the volume real/virtual size.
        sdCache.produce(sdUUID) \
               .produceVolume(imgUUID, volUUID).syncMetadata()

    def extendSize(self, newSize):
        """
        Extend the size (virtual disk size seen by the guest) of the volume.
        """
        if self.isShared():
            raise se.VolumeNonWritable(self.volUUID)

        volFormat = self.getFormat()
        if volFormat == sc.COW_FORMAT:
            self.log.debug("skipping cow size extension for volume %s to "
                           "size %s", self.volUUID, newSize)
            return
        elif volFormat != sc.RAW_FORMAT:
            raise se.IncorrectFormat(self.volUUID)

        # Note: This function previously prohibited extending non-leaf volumes.
        # If a disk is enlarged a volume may become larger than its parent.  In
        # order to support live merge of a larger volume into its raw parent we
        # must permit extension of this raw volume prior to starting the merge.
        isBase = self.getParent() == sc.BLANK_UUID
        if not (isBase or self.isLeaf()):
            raise se.VolumeNonWritable(self.volUUID)

        curRawSize = self.getVolumeSize()

        if (newSize < curRawSize):
            self.log.error("current size of volume %s is larger than the "
                           "size requested in the extension (%s > %s)",
                           self.volUUID, curRawSize, newSize)
            raise se.VolumeResizeValueError(newSize)

        if (newSize == curRawSize):
            self.log.debug("the requested size %s is equal to the current "
                           "size %s, skipping extension", newSize,
                           curRawSize)
        else:
            self.log.info("executing a raw size extension for volume %s "
                          "from size %s to size %s", self.volUUID,
                          curRawSize, newSize)
            vars.task.pushRecovery(task.Recovery(
                "Extend size for volume: " + self.volUUID, "volume",
                "Volume", "extendSizeFinalize",
                [self.sdUUID, self.imgUUID, self.volUUID]))
            self._extendSizeRaw(newSize)

        self.syncMetadata()  # update the metadata

    def setDescription(self, descr):
        self._manifest.setDescription(descr)

    def getDescription(self):
        return self._manifest.getDescription()

    def getLegality(self):
        return self._manifest.getLegality()

    def setLegality(self, legality):
        self._manifest.setLegality(legality)

    def setDomain(self, sdUUID):
        return self._manifest.setDomain(sdUUID)

    def setShared(self):
        return self._manifest.setShared()

    @deprecated  # valid for domain version < 3
    def setrw(self, rw):
        self._manifest.setrw(rw)

    def setLeaf(self):
        return self._manifest.setLeaf()

    def setInternal(self):
        return self._manifest.setInternal()

    def getVolType(self):
        return self._manifest.getVolType()

    def getSize(self):
        return self._manifest.getSize()

    def getVolumeSize(self, bs=sc.BLOCK_SIZE):
        return self._manifest.getVolumeSize(bs)

    def getVolumeTrueSize(self, bs=sc.BLOCK_SIZE):
        return self._manifest.getVolumeTrueSize(bs)

    def setSize(self, size):
        self._manifest.setSize(size)

    def updateInvalidatedSize(self):
        self._manifest.updateInvalidatedSize()

    def getType(self):
        return self._manifest.getType()

    def setType(self, prealloc):
        self._manifest.setType(prealloc)

    def getDiskType(self):
        return self._manifest.getDiskType()

    def getFormat(self):
        return self._manifest.getFormat()

    def setFormat(self, volFormat):
        self._manifest.setFormat(volFormat)

    def isLegal(self):
        return self._manifest.isLegal()

    def isFake(self):
        return self._manifest.isFake()

    def isShared(self):
        return self._manifest.isShared()

    def isLeaf(self):
        return self._manifest.isLeaf()

    def isInternal(self):
        return self._manifest.isInternal()

    def isSparse(self):
        return self._manifest.isSparse()

    def recheckIfLeaf(self):
        """
        Recheck if I am a leaf.
        """
        return self._manifest.recheckIfLeaf()

    @contextmanager
    def scopedPrepare(self, rw=True, justme=False, chainrw=False, setrw=False,
                      force=False):
        self.prepare(rw=True, justme=False, chainrw=False, setrw=False,
                     force=False)
        try:
            yield self
        finally:
            self.teardown(self.sdUUID, self.volUUID, justme)

    def prepare(self, rw=True, justme=False,
                chainrw=False, setrw=False, force=False):
        return self._manifest.prepare(rw, justme, chainrw, setrw, force)

    @classmethod
    def teardown(cls, sdUUID, volUUID, justme=False):
        return cls.manifestClass.teardown(sdUUID, volUUID, justme)

    def metadata2info(self, meta):
        return self._manifest.metadata2info(meta)

    @classmethod
    def newMetadata(cls, metaId, sdUUID, imgUUID, puuid, size, format, type,
                    voltype, disktype, desc="", legality=sc.ILLEGAL_VOL):
        return cls.manifestClass.newMetadata(
            metaId, sdUUID, imgUUID, puuid, size, format, type, voltype,
            disktype, desc, legality)

    def getInfo(self):
        return self._manifest.getInfo()

    def getParentVolume(self):
        """
        Return parent Volume object
        """
        puuid = self.getParent()
        if puuid and puuid != sc.BLANK_UUID:
            return sdCache.produce(self.sdUUID).produceVolume(self.imgUUID,
                                                              puuid)
        return None

    def setParent(self, puuid):
        """
        Set the parent volume UUID.  This information can be stored in multiple
        places depending on the underlying volume type.
        """
        self.setParentTag(puuid)
        self.setParentMeta(puuid)

    def getVolumePath(self):
        return self._manifest.getVolumePath()

    def getVmVolumeInfo(self):
        return self._manifest.getVmVolumeInfo()

    def getMetaParam(self, key):
        """
        Get a value of a specific key
        """
        return self._manifest.getMetaParam(key)

    def setMetaParam(self, key, value):
        """
        Set a value of a specific key
        """
        self._manifest.setMetaParam(key, value)

    def getVolumeParams(self, bs=sc.BLOCK_SIZE):
        return self._manifest.getVolumeParams(bs)

    def shrinkToOptimalSize(self):
        """
        Shrink only block volume of snapshot
        by reducing the lv to minimal size required
        """
        pass

    @classmethod
    def createMetadata(cls, metaId, meta):
        return cls.manifestClass.createMetadata(metaId, meta)

    @classmethod
    def newVolumeLease(cls, metaId, sdUUID, volUUID):
        return cls.manifestClass.newVolumeLease(metaId, sdUUID, volUUID)

    @classmethod
    def getImageVolumes(cls, repoPath, sdUUID, imgUUID):
        return cls.manifestClass.getImageVolumes(repoPath, sdUUID, imgUUID)
