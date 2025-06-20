# Module cloud-video-upload

This module implements a generic video upload service that periodically invokes a configured video-store `save` command to capture recent video segments and uploads them either to Viam Cloud or to Google Cloud Storage. It supports optional game-time scheduling windows and automatic cleanup of local files after successful upload.

## Model bill\:cloud-video-upload\:video-upload

The `video-upload` model defines a GenericService that:

* Validates required configuration for either Viam Cloud or GCP upload modes.
* Registers an interval-based scheduler to trigger video saving and uploading.
* Buffers end timestamps to avoid capturing in-progress segments.
* Optionally skips cycles when outside defined schedule windows.
* Cleans up local `.mp4` files after successful uploads.

### Configuration

The following attribute template can be used to configure this model:

```json
{
  "upload": <string>,
  "video_store": <string>,
  "interval": <number>,
  "upload_path": <string>,           // GCP-only
  "path_to_service_account": <string>,// GCP-only
  "google_cloud_path": <string>,     // GCP-only
  "schedule": [                      // optional
    { "start": <ISO8601>, "end": <ISO8601> }
  ]
}
```

#### Attributes

The following attributes are available for this model:

| Name                      | Type             | Inclusion             | Description                                                                                |
| ------------------------- | ---------------- | --------------------- | ------------------------------------------------------------------------------------------ |
| `upload`                  | string           | Required              | Choose `"viam-cloud"` or `"gcp-project"` mode                                              |
| `video_store`             | string           | Required              | The resource name of the video-store component to invoke `save` on                         |
| `interval`                | number (minutes) | Required              | Minutes between each upload cycle                                                          |
| `upload_path`             | string           | Required for GCP mode | Local filesystem path where saved segments are written                                     |
| `path_to_service_account` | string           | Required for GCP mode | Filesystem path to your Google Cloud service account JSON key file                         |
| `google_cloud_path`       | string           | Required for GCP mode | GCS bucket name and optional prefix (e.g. "my-bucket/videos")                              |
| `schedule`                | array of objects | Optional              | List of UTC windows to allow uploads; each object has `start` and `end` ISO8601 timestamps |

#### Example Configuration

##### Viam Cloud mode

```json
{
  "upload": "viam-cloud",
  "video_store": "myVideoStore1",
  "interval": 5
}
```

##### GCP mode

```json
{
  "upload": "gcp-project",
  "video_store": "myVideoStore1",
  "interval": 5,
  "upload_path": "/tmp/videos",
  "path_to_service_account": "/path/to/key.json",
  "google_cloud_path": "my-bucket/videos",
  "schedule": [
    { "start": "2025-06-20T18:00:00Z", "end": "2025-06-20T21:30:00Z" }
  ]
}
```

### DoCommand

This model does *not* implement custom `DoCommand` operations. All functionality is exposed via configuration and the scheduled upload cycle.
