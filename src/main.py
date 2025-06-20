import asyncio
from viam.module.module import Module
try:
    from models.video_upload import VideoUpload
except ModuleNotFoundError:
    # when running as local module with run.sh
    from .models.video_upload import VideoUpload


if __name__ == '__main__':
    asyncio.run(Module.run_from_registry())
