from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class RejectFileUploads(FileUploadHandler):
    """Refuse every multipart file part.

    File uploads use a raw request body (see ``files.api.upload``). Django's
    CSRF middleware parses multipart POST bodies *before* any view runs, so
    without this handler an unauthenticated client could make the coordinator
    spool arbitrarily large multipart files to disk. With it, file parts are
    dropped immediately; small form fields are still bounded by
    ``DATA_UPLOAD_MAX_MEMORY_SIZE``.
    """

    def new_file(self, *args, **kwargs):
        raise StopUpload(connection_reset=True)

    def receive_data_chunk(self, raw_data, start):  # pragma: no cover - never reached
        return None

    def file_complete(self, file_size):  # pragma: no cover - never reached
        return None
