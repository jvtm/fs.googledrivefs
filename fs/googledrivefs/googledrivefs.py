from __future__ import absolute_import

from datetime import datetime, timezone
from io import BytesIO, SEEK_END
from logging import getLogger
from os import close, remove
from os.path import splitext
from tempfile import mkstemp

from googleapiclient.discovery import build
from googleapiclient.http import DEFAULT_CHUNK_SIZE, MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
from fs.base import FS
from fs.enums import ResourceType
from fs.errors import DestinationExists, DirectoryExists, DirectoryExpected, DirectoryNotEmpty, FileExists, FileExpected, InvalidCharsInPath, NoURL, ResourceNotFound, OperationFailed, RemoveRootError
from fs.info import Info
from fs.iotools import RawWrapper
from fs.mode import Mode
from fs.path import basename, dirname, iteratepath, join, split
from fs.subfs import SubFS
from fs.time import datetime_to_epoch, epoch_to_datetime

_fileMimeType = 'application/vnd.google-apps.file'
_folderMimeType = 'application/vnd.google-apps.folder'
_shortcutMimeType = 'application/vnd.google-apps.shortcut'
_sharingUrl = 'https://drive.google.com/open?id='
_INVALID_PATH_CHARS = ':\0'
_log = getLogger('fs.googledrivefs')
_rootMetadata = {'id': 'root', 'mimeType': _folderMimeType}
_ALL_FIELDS = 'id,mimeType,kind,name,createdTime,modifiedTime,size,permissions,appProperties,contentHints,md5Checksum'

def _Escape(name):
	name = name.replace('\\', '\\\\')
	name = name.replace("'", r"\'")
	return name

def _CheckPath(path):
	for char in _INVALID_PATH_CHARS:
		if char in path:
			raise InvalidCharsInPath(path)
	if path.startswith('/'):
		return path[1:]
	return path

# TODO - switch to MediaIoBaseUpload and use BytesIO
class _UploadOnClose(RawWrapper):
	def __init__(self, fs, path, thisMetadata, parentMetadata, parsedMode, **options): # pylint: disable=too-many-arguments
		self.fs = fs
		self.path = path
		self.parentMetadata = parentMetadata
		# None here means we'll have to create a new file later
		self.thisMetadata = thisMetadata
		# keeping a parsed mode separate from the base class's mode member
		self.parsedMode = parsedMode
		self.options = options
		fileHandle, self.localPath = mkstemp(prefix='pyfilesystem-googledrive-', suffix=splitext(self.path)[1],
											 text=False)
		close(fileHandle)
		_log.debug(f'self.localPath: {self.localPath}')

		if (self.parsedMode.reading or self.parsedMode.appending) and not self.parsedMode.truncate:
			if self.thisMetadata is not None:
				with open(self.localPath, 'wb') as f:
					self.fs._download_to_file(path, self.thisMetadata, f)
		platformMode = self.parsedMode.to_platform()
		platformMode += ('b' if 'b' not in platformMode else '')
		platformMode = platformMode.replace('x', 'a')
		super().__init__(f=open(self.localPath, mode=platformMode))
		if self.parsedMode.appending:
			# seek to the end
			self.seek(0, SEEK_END)

	def close(self):
		super().close()  # close the file so that it's readable for upload
		if self.parsedMode.writing:
			# google doesn't accept the fractional second part
			now = datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
			uploadMetadata = {'modifiedTime': now}
			if self.thisMetadata is None:
				uploadMetadata.update(
					{'name': basename(self.path), 'parents': [self.parentMetadata['id']], 'createdTime': now})
				if 'createdDateTime' in self.options:
					uploadMetadata.update(
						{'createdTime': self.options['createdDateTime'].replace(microsecond=0).isoformat() + 'Z'})

			with open(self.localPath, 'rb') as f:
				dataToWrite = f.read()
			_log.debug(f'About to upload data: {dataToWrite}')

			if len(dataToWrite) > 0:
				upload = MediaFileUpload(self.localPath, resumable=True)
				if self.thisMetadata is None:
					_log.debug('Creating new file')
					request = self.fs.drive.files().create(body=uploadMetadata, media_body=upload)
				else:
					_log.debug('Updating existing file')
					request = self.fs.drive.files().update(fileId=self.thisMetadata['id'], body={}, media_body=upload)

				response = None
				while response is None:
					status, response = request.next_chunk()
					_log.debug(f'{status}: {response}')
				# MediaFileUpload doesn't close it's file handle, so we have to workaround it (https://github.com/googleapis/google-api-python-client/issues/575)
				upload._fd.close()  # pylint: disable=protected-access
			else:
				fh = BytesIO(b'')
				media = MediaIoBaseUpload(fh, mimetype='application/octet-stream', chunksize=-1, resumable=False)
				if self.thisMetadata is None:
					createdFile = self.fs.drive.files().create(
						body=uploadMetadata,
						media_body=media).execute(num_retries=self.fs.retryCount)
					_log.debug(f'Created empty file: {createdFile}')
				else:
					updatedFile = self.fs.drive.files().update(
						fileId=self.thisMetadata['id'],
						body={},
						media_body=media).execute(num_retries=self.fs.retryCount)
					_log.debug(f'Updated file to empty: {updatedFile}')
		remove(self.localPath)

class SubGoogleDriveFS(SubFS):
	def __repr__(self):
		return '<SubGoogleDriveFS>'

	def add_shortcut(self, shortcut_path, target_path):
		fs, shortcutPathDelegate = self.delegate_path(shortcut_path)
		fs, targetPathDelegate = self.delegate_path(target_path)
		fs.add_shortcut(shortcutPathDelegate, targetPathDelegate)

class GoogleDriveFS(FS):
	subfs_class = SubGoogleDriveFS

	def __init__(self, credentials, rootId=None):
		super().__init__()

		self.drive = build('drive', 'v3', credentials=credentials, cache_discovery=False)
		self.retryCount = 3
		self.rootId = rootId

		_meta = self._meta = {
			'case_insensitive': True, # it will even let you have 2 identical filenames in the same directory! But the search is case-insensitive
			'invalid_path_chars': _INVALID_PATH_CHARS,  # not sure what else
			'max_path_length': None,  # don't know what the limit is
			'max_sys_path_length': None,  # there's no syspath
			'network': True,
			'read_only': False,
			'supports_rename': False  # since we don't have a syspath...
		}

	def __repr__(self):
		return '<GoogleDriveFS>'

	def search(self, condition):
		_log.info(f'search: {condition()}')
		rawResults = self._fileQuery(condition())
		return (self._infoFromMetadata(x) for x in rawResults)

	def _fileQuery(self, query):
		allFields = f'nextPageToken,files({_ALL_FIELDS})'
		response = self.drive.files().list(q=query, fields=allFields).execute(num_retries=self.retryCount)
		result = response['files']
		while 'nextPageToken' in response:
			response = self.drive.files().list(q=query, fields=allFields, pageToken=response['nextPageToken']).execute(num_retries=self.retryCount)
			result.extend(response['files'])
		return result

	def _childByName(self, parentId, childName):
		if not parentId:
			parentId = 'root'
		# this "name=" clause seems to be case-insensitive, which means it's easier to model this as a case-insensitive filesystem
		result = self._fileQuery(f"trashed=False and name='{_Escape(childName)}' and '{parentId}' in parents")
		if len(result) not in [0, 1]:
			# Google drive doesn't follow the model of a filesystem, really
			# but since most people will set it up to follow the model, we'll carry on regardless
			# and just throw an error when it becomes a problem
			raise RuntimeError(f'Folder with id {parentId} has more than 1 child with name {childName}')
		return result[0] if len(result) == 1 else None

	def _childrenById(self, parentId):
		return self._fileQuery(f"trashed=False and '{parentId}' in parents")

	def _itemsFromPath(self, path):
		pathIdMap = {'': _rootMetadata}
		ipath = iteratepath(path)

		pathSoFar = ''
		parentId = self.rootId

		if self.rootId is not None:
			# if we have been given a `rootId` then get the info for this directory and set it as
			# the root directory's metadata.
			rootMetadata = self.drive.files().get(fileId=self.rootId, fields=_ALL_FIELDS).execute()
			if rootMetadata is None:
				return pathIdMap
			pathIdMap[''] = rootMetadata

		for childName in ipath:
			pathSoFar = join(pathSoFar, childName)
			metadata = self._childByName(parentId, childName)
			if metadata is None:
				break

			pathIdMap[pathSoFar] = metadata
			parentId = metadata['id'] # pylint: disable=unsubscriptable-object

		return pathIdMap

	def _itemFromPath(self, path):
		pathIdMap = self._itemsFromPath(path)
		return pathIdMap.get(path)

	def _infoFromMetadata(self, metadata):  # pylint: disable=no-self-use
		isRoot = (metadata == _rootMetadata)
		isFolder = (metadata['mimeType'] == _folderMimeType)
		rfc3339 = '%Y-%m-%dT%H:%M:%S.%fZ'
		permissions = metadata.get('permissions', None)
		rawInfo = {
			'basic': {
				'name': '' if isRoot else metadata['name'],
				'is_dir': isFolder
			},
			'details': {
				'accessed': None,  # not supported by Google Drive API
				'created': None if isRoot else datetime_to_epoch(datetime.strptime(metadata['createdTime'], rfc3339)),
				'metadata_changed': None,  # not supported by Google Drive API
				'modified': None if isRoot else datetime_to_epoch(datetime.strptime(metadata['modifiedTime'], rfc3339)),
				'size': int(metadata['size']) if 'size' in metadata else None, # folders, native google documents etc have no size
				'type': ResourceType.directory if isFolder else ResourceType.file
			},
			'sharing': {
				'id': metadata['id'],
				'permissions': permissions,
				'is_shared': len(permissions) > 1 if permissions is not None else None
			}
		}
		googleMetadata = {}
		if 'contentHints' in metadata and 'indexableText' in metadata['contentHints']:
			googleMetadata.update({'indexableText': metadata['contentHints']['indexableText']})
		if 'appProperties' in metadata:
			googleMetadata.update({'appProperties': metadata['appProperties']})
		if 'md5Checksum' in metadata:
			rawInfo.update({'hashes': {'MD5': metadata['md5Checksum']}})
		if 'mimeType' in metadata:
			googleMetadata.update({'isShortcut': metadata['mimeType'] == _shortcutMimeType})
		rawInfo.update({'google': googleMetadata})
		# there is also file-type-specific metadata like imageMediaMetadata
		return Info(rawInfo)

	def getinfo(self, path, namespaces=None):  # pylint: disable=unused-argument
		path = _CheckPath(path)
		with self._lock:
			metadata = self._itemFromPath(path)
			if metadata is None:
				raise ResourceNotFound(path=path)
			return self._infoFromMetadata(metadata)

	def setinfo(self, path, info):  # pylint: disable=redefined-outer-name,too-many-branches,unused-argument
		path = _CheckPath(path)
		with self._lock:
			metadata = self._itemFromPath(path)
			if metadata is None:
				raise ResourceNotFound(path=path)
			updatedData = {}
			for namespace in info:
				for name, value in info[namespace].items():
					if namespace == 'details':
						if name == 'modified':
							# incoming datetimes should be utc timestamps, Google Drive expects RFC 3339
							updatedData['modifiedTime'] = epoch_to_datetime(value).replace(tzinfo=timezone.utc).isoformat()
					elif namespace == 'google':
						if name == 'indexableText':
							updatedData['contentHints'] = {'indexableText': value}
						elif name == 'appProperties':
							assert isinstance(value, dict)
							updatedData['appProperties'] = value
			self.drive.files().update(fileId=metadata['id'], body=updatedData).execute(num_retries=self.retryCount)

	def share(self, path, email=None, role='reader'):
		"""
		Shares item.
		:param path: item path
		:param email: email of gmail-user to share item. If None, will share with anybody.
		:param role: google drive sharing role
		:return: URL
		"""
		path = _CheckPath(path)
		with self._lock:
			metadata = self._itemFromPath(path)
			if metadata is None:
				raise ResourceNotFound(path=path)
			if role not in ('reader', 'writer', 'commenter', 'fileOrganizer', 'organizer', 'owner'):
				raise OperationFailed(path=path, msg=f'unknown sharing role: {role}')
			if email:
				permissions = {'role': role, 'type': 'user', 'emailAddress': email}
			else:
				permissions = {'role': role, 'type': 'anyone'}
			self.drive.permissions().create(fileId=metadata['id'], body=permissions).execute(num_retries=self.retryCount)
			return self.geturl(path)

	def hasurl(self, path, purpose='download'):
		path = _CheckPath(path)
		if purpose != 'download':
			raise NoURL(path, purpose, 'No such purpose')
		with self._lock:
			try:
				return self.getinfo(path).get('sharing', 'is_shared')
			except ResourceNotFound:
				return False

	def geturl(self, path, purpose='download'): # pylint: disable=unused-argument
		path = _CheckPath(path)
		if purpose != 'download':
			raise NoURL(path, purpose, 'No such purpose')
		with self._lock:
			fileInfo = self.getinfo(path)
			if fileInfo.get('sharing', 'is_shared') is False:
				raise NoURL(path, purpose, f'{path} is not shared')
			return _sharingUrl + fileInfo.get('sharing', 'id')

	def listdir(self, path):
		path = _CheckPath(path)
		with self._lock:
			return [x.name for x in self.scandir(path)]

	def _createSubdirectory(self, name, path, parents):
		newMetadata = {'name': basename(name), 'parents': parents, 'mimeType': _folderMimeType, 'enforceSingleParent': True}
		self.drive.files().create(body=newMetadata, fields='id').execute(num_retries=self.retryCount)
		return SubGoogleDriveFS(self, path)

	def makedir(self, path, permissions=None, recreate=False):
		path = _CheckPath(path)
		with self._lock:
			_log.info(f'makedir: {path}, {permissions}, {recreate}')
			parentMetadata = self._itemFromPath(dirname(path))

			if parentMetadata is None:
				raise ResourceNotFound(path=path)

			childMetadata = self._childByName(parentMetadata['id'], basename(path))
			if childMetadata is not None:
				if recreate is False:
					raise DirectoryExists(path=path)
				return SubGoogleDriveFS(self, path)

			return self._createSubdirectory(basename(path), path, [parentMetadata['id']])

	def openbin(self, path, mode='r', buffering=-1, **options):  # pylint: disable=unused-argument
		path = _CheckPath(path)
		with self._lock:
			_log.info(f'openbin: {path}, {mode}, {buffering}')
			parsedMode = Mode(mode)
			idsFromPath = self._itemsFromPath(path)
			item = idsFromPath.get(path)
			if parsedMode.exclusive and item is not None:
				raise FileExists(path)
			if parsedMode.reading and not parsedMode.create and item is None:
				raise ResourceNotFound(path)
			if item is not None and item['mimeType'] == _folderMimeType:
				raise FileExpected(path)
			parentDir = dirname(path)
			_log.debug(f'looking up id for {parentDir} in {list(idsFromPath.keys())}')
			parentDirItem = idsFromPath.get(parentDir)
			# make sure that the parent directory exists if we're writing
			if parsedMode.writing and parentDirItem is None:
				raise ResourceNotFound(parentDir)
			return _UploadOnClose(fs=self, path=path, thisMetadata=item, parentMetadata=parentDirItem, parsedMode=parsedMode, **options)

	def _download_to_file(self, path, metadata, file_obj, chunk_size=None):
		_log.info('download %r', path)
		assert metadata is not None
		if chunk_size is None:
			chunk_size = DEFAULT_CHUNK_SIZE
		request = self.drive.files().get_media(fileId=metadata['id'])
		downloader = MediaIoBaseDownload(file_obj, request, chunksize=chunk_size)
		done = False
		while not done:
			status, done = downloader.next_chunk(num_retries=self.retryCount)
			_log.debug('download status %r %s/%s bytes (%.1f%%)', path, status.resumable_progress,
					   status.total_size, status.progress() * 100)

	def download(self, path, file, chunk_size=None, **options):
		path = _CheckPath(path)
		with self._lock:
			metadata = self._itemFromPath(path)
			if metadata is None:
				raise ResourceNotFound(path)
			self._download_to_file(path, metadata, file, chunk_size=chunk_size)

	def remove(self, path):
		if path == '/':
			raise RemoveRootError()
		path = _CheckPath(path)
		with self._lock:
			_log.info(f'remove: {path}')
			metadata = self._itemFromPath(path)
			if metadata is None:
				raise ResourceNotFound(path=path)
			if metadata['mimeType'] == _folderMimeType:
				raise FileExpected(path=path)
			self.drive.files().delete(fileId=metadata['id']).execute(num_retries=self.retryCount)

	def removedir(self, path):
		if path == '/':
			raise RemoveRootError()
		path = _CheckPath(path)
		with self._lock:
			_log.info(f'removedir: {path}')
			metadata = self._itemFromPath(path)
			if metadata is None:
				raise ResourceNotFound(path=path)
			if metadata['mimeType'] != _folderMimeType:
				raise DirectoryExpected(path=path)
			children = self._childrenById(metadata['id'])
			if len(children) > 0:
				raise DirectoryNotEmpty(path=path)
			self.drive.files().delete(fileId=metadata['id']).execute(num_retries=self.retryCount)

	def _generateChildren(self, children, page):
		if page:
			return (self._infoFromMetadata(x) for x in children[page[0]:page[1]])
		return (self._infoFromMetadata(x) for x in children)

	# Non-essential method - for speeding up walk
	# Takes advantage of the fact that you get the full metadata for all children in one call
	def scandir(self, path, namespaces=None, page=None):
		path = _CheckPath(path)
		with self._lock:
			_log.info(f'scandir: {path}, {namespaces}, {page}')
			metadata = self._itemFromPath(path)
			if metadata is None:
				raise ResourceNotFound(path=path)

			if metadata['mimeType'] != _folderMimeType:
				raise DirectoryExpected(path=path)

			children = self._childrenById(metadata['id'])
			return self._generateChildren(children, page)

	# Non-essential - takes advantage of the file contents are already being on the server
	def copy(self, src_path, dst_path, overwrite=False):
		_log.info(f'copy: {src_path} -> {dst_path}, {overwrite}')
		src_path = _CheckPath(src_path)
		dst_path = _CheckPath(dst_path)
		with self._lock:
			parentDir = dirname(dst_path)
			parentDirItem = self._itemFromPath(parentDir)

			if parentDirItem is None:
				raise ResourceNotFound(parentDir)

			dstItem = self._itemFromPath(dst_path)
			if overwrite is False and dstItem is not None:
				raise DestinationExists(dst_path)

			srcItem = self._itemFromPath(src_path)
			if srcItem is None:
				raise ResourceNotFound(src_path)

			if srcItem['mimeType'] == _folderMimeType:
				raise FileExpected(src_path)

			# TODO - we should really replace the contents of the existing file with the new contents, so that the history is correct
			if dstItem is not None:
				self.drive.files().delete(fileId=dstItem['id']).execute(num_retries=self.retryCount)

			newMetadata = {'parents': [parentDirItem['id']], 'name': basename(dst_path), 'enforceSingleParent': True}
			self.drive.files().copy(fileId=srcItem['id'], body=newMetadata).execute(num_retries=self.retryCount)

	# Non-essential - takes advantage of the file contents already being on the server
	def move(self, src_path, dst_path, overwrite=False):
		_log.info(f'move: {src_path} -> {dst_path}, {overwrite}')
		src_path = _CheckPath(src_path)
		dst_path = _CheckPath(dst_path)
		with self._lock:
			dstItem = self._itemFromPath(dst_path)
			if overwrite is False and dstItem is not None:
				raise DestinationExists(dst_path)

			srcParentItem = self._itemFromPath(dirname(src_path))
			if srcParentItem is None:
				raise ResourceNotFound(src_path)

			# TODO - it would be more efficient to go directly from srcParentItem to it's child here
			srcItem = self._itemFromPath(src_path)
			if srcItem is None:
				raise ResourceNotFound(src_path)

			if srcItem['mimeType'] == _folderMimeType:
				raise FileExpected(src_path)

			dstParentDir = dirname(dst_path)
			dstParentDirItem = self._itemFromPath(dstParentDir)

			if dstParentDirItem is None:
				raise ResourceNotFound(dstParentDir)

			if dstItem is not None:
				assert overwrite is True
				self.drive.files().delete(fileId=dstItem['id']).execute(num_retries=self.retryCount)

			self.drive.files().update(
				fileId=srcItem['id'],
				addParents=dstParentDirItem['id'],
				removeParents=srcParentItem['id'],
				body={'name': basename(dst_path), 'enforceSingleParent': True}).execute(num_retries=self.retryCount)

	def add_shortcut(self, shortcut_path, target_path):
		_log.info(f'add_shortcut: {shortcut_path}, {target_path}')
		shortcut_path = _CheckPath(shortcut_path)
		target_path = _CheckPath(target_path)

		with self._lock:
			idsFromTargetPath = self._itemsFromPath(target_path)
			if target_path not in idsFromTargetPath:
				raise ResourceNotFound(path=target_path)

			targetItem = idsFromTargetPath[target_path]
			if targetItem['mimeType'] == _folderMimeType:
				raise FileExpected(target_path)

			idsFromShortcutPath = self._itemsFromPath(shortcut_path)
			if shortcut_path in idsFromShortcutPath:
				raise DestinationExists(shortcut_path)

			shortcutParentDir, shortcutName = split(shortcut_path)
			shortcutParentDirItem = idsFromShortcutPath.get(shortcutParentDir)
			if shortcutParentDirItem is None:
				raise ResourceNotFound(shortcutParentDir)

			metadata = {
				'name': shortcutName,
				'parents': [shortcutParentDirItem['id']],
				'mimeType': _shortcutMimeType,
				'shortcutDetails': {
					'targetId': targetItem['id']
				},
				'enforceSingleParent': True
			}

			_ = self.drive.files().create(body=metadata, fields='id').execute(num_retries=self.retryCount)
