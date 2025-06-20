import asyncio
import os
import time
import math
import requests
from datetime import datetime, timedelta, timezone
from typing import ClassVar, Mapping, Optional, Sequence, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from typing_extensions import Self

from viam.module.module import Module
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.services.generic import Generic as GenericService
from viam.components.generic import Generic as GenericComponent
from viam.components.camera import Camera
from viam.utils import ValueTypes
from viam.logging import getLogger
from google.cloud import storage
from google.oauth2 import service_account

LOGGER = getLogger(__name__)

class VideoUpload(GenericService, EasyResource):
    # To enable debug-level logging, either run viam-server with the --debug option,
    # or configure your resource/machine to display debug logs.
    MODEL: ClassVar[Model] = Model(
        ModelFamily("bill", "cloud-video-upload"), "video-upload"
    )

    BUFFER_SECONDS: ClassVar[int] = 30  # seconds to back off from 'now' for end timestamp

    @classmethod
    def new(
        cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        """This method creates a new instance of this Generic service.
        The default implementation sets the name from the `config` parameter and then calls `reconfigure`.

        Args:
            config (ComponentConfig): The configuration for this resource
            dependencies (Mapping[ResourceName, ResourceBase]): The dependencies (both required and optional)

        Returns:
            Self: The resource
        """
        return super().new(config, dependencies)

    @classmethod
    def validate_config(
        cls, config: ComponentConfig
    ) -> Tuple[Sequence[str], Sequence[str]]:
        """This method allows you to validate the configuration object received from the machine,
        as well as to return any required dependencies or optional dependencies based on that `config`.

        Args:
            config (ComponentConfig): The configuration for this resource

        Returns:
            Tuple[Sequence[str], Sequence[str]]: A tuple where the
                first element is a list of required dependencies and the
                second element is a list of optional dependencies
        """
        LOGGER.info(f"[{cls.__name__}] Validating configuration...")
        fields = config.attributes.fields
        # required for both modes
        for key in ('upload', 'video_store', 'interval'):
            if key not in fields:
                raise ValueError(f"Missing config attribute '{key}'")
        mode = fields['upload'].string_value
        if mode not in ('viam-cloud', 'gcp-project'):
            raise ValueError("'upload' must be 'viam-cloud' or 'gcp-project'")
        # additional for GCP mode
        if mode == 'gcp-project':
            for key in ('upload_path', 'path_to_service_account', 'google_cloud_path'):
                if key not in fields:
                    raise ValueError(f"Missing config attribute '{key}' for gcp-project mode")
        # Return the video store as a required dependency
        # and no optional dependencies
        LOGGER.info(f"[{cls.__name__}] Configuration validated successfully.")
        return [fields['video_store'].string_value], []

    def reconfigure(
        self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ):
        """This method allows you to dynamically update your service when it receives a new `config` object.

        Args:
            config (ComponentConfig): The new configuration
            dependencies (Mapping[ResourceName, ResourceBase]): Any dependencies (both required and optional)
        """
        LOGGER.info(f"[{self.name}] Reconfiguring module...")
        # shut down any existing scheduler
        if hasattr(self, 'scheduler') and self.scheduler:
            self.scheduler.shutdown()

        fields = config.attributes.fields

        # ─── Load game‐time schedule if present ────────────────────────────────────
        if 'schedule' in fields:
            raw = fields['schedule'].list_value.values
            self.schedule = [
                {
                    'start': item.struct_value.fields['start'].string_value,
                    'end':   item.struct_value.fields['end'].string_value,
                }
                for item in raw
            ]
        else:
            self.schedule = []
        LOGGER.info(f"[{self.name}] Loaded {len(self.schedule)} game windows")

        # common attributes
        self.upload_mode = fields['upload'].string_value
        vs_name = fields['video_store'].string_value
        LOGGER.info(f"[{self.name}] dependencies keys: {', '.join(str(k) for k in dependencies.keys())}")
        for k, v in dependencies.items():
            LOGGER.debug(f"[{self.name}]   {k!r} → {v!r}")
        self.video_store = dependencies[GenericComponent.get_resource_name(vs_name)]
        self.local_path = fields['upload_path'].string_value
        self.interval = int(fields['interval'].number_value)

        # GCP-specific setup
        if self.upload_mode == 'gcp-project':
            if storage is None:
                raise ImportError("google-cloud-storage not installed but required for gcp-project")

            # load credentials from the service-account JSON
            creds_path = fields['path_to_service_account'].string_value
            creds = service_account.Credentials.from_service_account_file(creds_path)
            client = storage.Client(credentials=creds, project=creds.project_id)

            # parse bucket name + optional prefix from google_cloud_path
            full_path = fields['google_cloud_path'].string_value.strip('/')
            parts = full_path.split('/', 1)
            bucket_name = parts[0]
            self.cloud_prefix = parts[1] if len(parts) > 1 else ""

            self.bucket = client.bucket(bucket_name)
            LOGGER.info(f"[{self.name}] GCS bucket initialized: {bucket_name}, prefix: '{self.cloud_prefix}'")

        # schedule periodic upload cycles
        self.scheduler = AsyncIOScheduler()
        first_run = datetime.now(timezone.utc) + timedelta(minutes=self.interval)
        self.scheduler.add_job(
            self.upload_cycle,
            trigger='interval',
            minutes=self.interval,
            id=f"{self.name}_interval_save",
            next_run_time=first_run
        )
        self.scheduler.start()
        LOGGER.info(f"[{self.name}] Scheduler started: first run at {first_run}")

        return super().reconfigure(config, dependencies)

    async def save_video(self) -> None:
        LOGGER.info(f"[{self.name}] Invoking video-store save command")
        now = datetime.now(timezone.utc)
        end_time = now - timedelta(seconds=self.BUFFER_SECONDS)
        start_time = end_time - timedelta(minutes=self.interval)
        command = {
            'command': 'save',
            'from': start_time.strftime("%Y-%m-%d_%H-%M-%SZ"),
            'to':   end_time.strftime("%Y-%m-%d_%H-%M-%SZ"),
        }
        response = await self.video_store.do_command(command)
        LOGGER.info(f"[{self.name}] Save response: {response}")

    def is_game_time(self, schedule: list[dict]) -> bool:
        """
        Given a list of {"start": iso‐string, "end": iso‐string} dicts,
        return True if now (UTC) falls inside any of the windows.
        """
        now = datetime.now(timezone.utc)
        for window in schedule:
            start = datetime.fromisoformat(window["start"]).astimezone(timezone.utc)
            end   = datetime.fromisoformat(window["end"]).astimezone(timezone.utc)
            if start <= now <= end:
                return True
        return False

    async def upload_cycle(self):
        # if there's a schedule configured, bail out when we're not in a game window
        if getattr(self, "schedule", None):
            if not self.is_game_time(self.schedule):
                LOGGER.info(f"[{self.name}] Not game time, skipping save.")
                return
            LOGGER.info(f"[{self.name}] Within game window, proceeding with save/upload.")

        LOGGER.info(f"[{self.name}] Upload cycle START")

        # Save video segments
        try:
            await self.save_video()
        except Exception as e:
            LOGGER.error(f"[{self.name}] Save failed: {e}", exc_info=True)
            return

        # If its a GCP upload, upload saved files to GCS
        if self.upload_mode == 'gcp-project':
            # small delay to ensure files are flushed to disk
            await asyncio.sleep(5)

            for root, _, files in os.walk(self.local_path):
                for fname in files:
                    if not fname.endswith('.mp4'):
                        continue

                    src = os.path.join(root, fname)
                    # construct destination path in bucket
                    dest_blob = f"{self.cloud_prefix}/{fname}" if self.cloud_prefix else fname
                    blob = self.bucket.blob(dest_blob)

                    try:
                        # precondition to avoid overwriting existing object
                        blob.upload_from_filename(src, if_generation_match=0)
                        os.remove(src)
                        LOGGER.info(
                            f"[{self.name}] Uploaded {fname} to gs://{self.bucket.name}/{dest_blob} "
                            "and removed local copy"
                        )
                    except Exception as e:
                        LOGGER.error(f"[{self.name}] GCS upload error for {fname}: {e}", exc_info=True)

        LOGGER.info(f"[{self.name}] Upload cycle END")

    async def close(self):
        LOGGER.info(f"[{self.name}] Shutting down module")
        if hasattr(self, 'scheduler') and self.scheduler:
            self.scheduler.shutdown()

    async def do_command(
        self,
        command: Mapping[str, ValueTypes],
        *,
        timeout: Optional[float] = None,
        **kwargs
    ) -> Mapping[str, ValueTypes]:
        self.logger.error("`do_command` is not implemented")
        raise NotImplementedError()

if __name__ == '__main__':
    asyncio.run(Module.run_from_registry())