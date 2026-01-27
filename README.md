# spa-deploy

A CLI tool that builds and deploys Vite or Yarn SPA projects to AWS S3, with optional CloudFront distribution.

## What it does

1. Detects your package manager (yarn or npm) and runs the build
2. Uploads the build output to an S3 bucket, creating the bucket if needed
3. Configures the bucket for static website hosting
4. Optionally creates a CloudFront distribution with Origin Access Control to front the bucket
5. On subsequent deploys, skips resource creation and invalidates the CloudFront cache
6. Tracks all created AWS resources in a `spa_deploy.json` state file in your project directory

## Installation

Requires Python 3.9+ and valid AWS credentials configured in your environment.

### With pipx (recommended)

```
pipx install .
```

### With pip

```
pip install .
```

### For development

```
pipx install -e .
```

## Usage

```
spa-deploy --bucket <bucket-name> [options]
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--bucket` | S3 bucket name (required) | |
| `--cloudfront` | Front the bucket with CloudFront | disabled |
| `--region` | AWS region | `us-east-1` |
| `--dir` | Project directory | current directory |
| `--output` | Build output directory | auto-detects `dist/` or `build/` |
| `--skip-build` | Skip the build step | disabled |

### Examples

Deploy to S3 as a static website:

```
spa-deploy --bucket my-app-prod
```

Deploy with CloudFront:

```
spa-deploy --bucket my-app-prod --cloudfront
```

Deploy a project in another directory, specifying region:

```
spa-deploy --bucket my-app-prod --dir ./my-project --region eu-west-1
```

Redeploy without rebuilding:

```
spa-deploy --bucket my-app-prod --skip-build
```

## State file

The tool writes a `spa_deploy.json` file to your project directory tracking created resources:

```json
{
  "bucket_name": "my-app-prod",
  "region": "us-east-1",
  "s3_website_url": "http://my-app-prod.s3-website-us-east-1.amazonaws.com",
  "cloudfront_distribution_id": "E1A2B3C4D5E6F7",
  "cloudfront_domain": "d1234abcdef.cloudfront.net",
  "created_resources": ["s3_bucket", "cloudfront_distribution"]
}
```

On subsequent runs the tool uses this file to skip resource creation and instead update the existing deployment (uploading new files and invalidating the CloudFront cache).

## How S3-only mode works

- The bucket is configured with public access and a static website hosting policy
- `index.html` is set as both the index and error document (for SPA client-side routing)
- HTML files are uploaded with `no-cache` headers; files under `assets/` get a one-year immutable cache

## How CloudFront mode works

- An Origin Access Control (OAC) is created so only CloudFront can read from the bucket
- The bucket is not publicly accessible — no static website hosting is configured
- A custom error response maps 403 to `/index.html` for SPA routing
- The CloudFront distribution uses the AWS managed `CachingOptimized` cache policy with compression enabled
- On redeployment, a `/*` cache invalidation is created automatically
