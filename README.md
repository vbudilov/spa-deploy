# spa-deploy

A CLI tool that builds and deploys Vite or Yarn SPA projects to AWS S3, with optional CloudFront CDN, custom domain, ACM TLS certificate, and Route53 DNS management.

## What it does

1. **Builds** your project — detects yarn (via `yarn.lock`) or falls back to npm, then runs `npm|yarn run build`
2. **Creates an S3 bucket** if it doesn't already exist
3. **Uploads** all build output files with correct content types and cache headers
4. **Configures static website hosting** on the bucket (S3-only mode), or
5. **Creates a CloudFront distribution** with Origin Access Control so the bucket stays private (CloudFront mode)
6. **Provisions an ACM TLS certificate** with automated DNS validation via Route53 (custom domain mode)
7. **Creates a Route53 A-record alias** pointing your custom domain to the CloudFront distribution
8. **Tracks all created resources** in a `spa_deploy.json` state file so subsequent deploys skip resource creation and `--destroy` knows what to tear down
9. **Invalidates the CloudFront cache** automatically on redeployment

## Prerequisites

- Python 3.9+
- AWS credentials configured in your environment (`~/.aws/credentials`, environment variables, or IAM role)
- The following IAM permissions (depending on features used):
  - **S3**: `s3:CreateBucket`, `s3:PutObject`, `s3:PutBucketPolicy`, `s3:PutBucketWebsite`, `s3:PutPublicAccessBlock`, `s3:HeadBucket`, `s3:DeleteBucket`, `s3:DeleteObject`, `s3:ListBucket`, `s3:ListBucketVersions`, `s3:DeleteBucketWebsite`
  - **CloudFront**: `cloudfront:CreateDistribution`, `cloudfront:CreateInvalidation`, `cloudfront:CreateOriginAccessControl`, `cloudfront:GetDistribution`, `cloudfront:UpdateDistribution`, `cloudfront:DeleteDistribution`, `cloudfront:GetOriginAccessControl`, `cloudfront:DeleteOriginAccessControl`
  - **ACM** (custom domain): `acm:RequestCertificate`, `acm:DescribeCertificate`, `acm:DeleteCertificate`
  - **Route53** (custom domain): `route53:ListHostedZonesByName`, `route53:ChangeResourceRecordSets`

## Installation

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

### Flags

#### `--bucket <name>` (required)

The S3 bucket name. The bucket is created if it does not exist. In CloudFront mode, this bucket serves as the origin.

#### `--cloudfront`

Front the S3 bucket with a CloudFront distribution. When enabled:

- The bucket is made private (no public website hosting)
- An Origin Access Control (OAC) is created so only CloudFront can read from the bucket
- A bucket policy scoped to the distribution's ARN is applied
- HTTPS is enforced via `redirect-to-https`
- The AWS-managed `CachingOptimized` cache policy is used with compression enabled
- A custom error response maps 403 to `/index.html` for SPA client-side routing
- On redeployment, a `/*` cache invalidation is issued automatically

You are prompted to confirm before the distribution is created.

#### `--domain <domain>`

Custom domain name (e.g. `app.example.com`). Requires `--cloudfront`. When provided, the script:

1. Finds the matching Route53 hosted zone (walks up the domain hierarchy, e.g. `app.example.com` matches zone `example.com`)
2. Requests a DNS-validated ACM certificate in `us-east-1` (required by CloudFront)
3. Creates the CNAME validation record in Route53 automatically
4. Waits for the certificate to be issued (typically 1-3 minutes)
5. Creates the CloudFront distribution with the domain as an alternate name (CNAME) and the ACM certificate attached (SNI, TLSv1.2+)
6. Creates a Route53 A-record alias pointing the domain to the CloudFront distribution

The hosted zone for your domain must already exist in Route53.

#### `--region <region>`

AWS region for the S3 bucket (default: `us-east-1`). ACM certificates for CloudFront are always created in `us-east-1` regardless of this setting.

#### `--dir <path>`

Path to the project directory (default: current directory). The script looks for `yarn.lock` here to decide between yarn and npm.

#### `--output <path>`

Path to the build output directory. If not specified, the script auto-detects by looking for `dist/` or `build/` inside the project directory. Use this when your build outputs to a non-standard location.

#### `--skip-build`

Skip the build step and deploy the existing output directory as-is. Useful for CI pipelines where the build is handled separately, or when redeploying without code changes.

#### `--destroy`

Tear down all AWS resources tracked in `spa_deploy.json`. Resources are removed in reverse dependency order:

1. Route53 A-record alias (pointing domain to CloudFront)
2. Route53 ACM validation CNAME record
3. CloudFront distribution (disabled first, then deleted after it finishes deploying)
4. Origin Access Control (OAC)
5. ACM certificate
6. S3 bucket (all objects and versions are deleted first)

You are prompted to confirm before anything is deleted. Each step handles errors gracefully and continues, so a partial teardown can be re-run. The state file is removed after successful completion.

### Examples

Deploy to S3 as a public static website:

```
spa-deploy --bucket my-app-prod
```

Deploy with CloudFront (HTTPS, global CDN):

```
spa-deploy --bucket my-app-prod --cloudfront
```

Deploy with CloudFront and a custom domain:

```
spa-deploy --bucket my-app-prod --cloudfront --domain app.example.com
```

Deploy a project in another directory with a specific region:

```
spa-deploy --bucket my-app-prod --dir ./my-project --region eu-west-1
```

Deploy a pre-built project from a specific output directory:

```
spa-deploy --bucket my-app-prod --skip-build --output ./dist
```

Redeploy (uploads new files + invalidates CloudFront cache):

```
spa-deploy --bucket my-app-prod --cloudfront --skip-build
```

Destroy all provisioned resources:

```
spa-deploy --bucket my-app-prod --destroy
```

## State file

All created resources are recorded in `spa_deploy.json` in the project directory:

```json
{
  "bucket_name": "my-app-prod",
  "region": "us-east-1",
  "s3_website_url": "http://my-app-prod.s3-website-us-east-1.amazonaws.com",
  "cloudfront_distribution_id": "E1A2B3C4D5E6F7",
  "cloudfront_domain": "d1234abcdef.cloudfront.net",
  "cloudfront_oac_id": "E9A8B7C6D5",
  "domain": "app.example.com",
  "acm_certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/abc-def-123",
  "route53_zone_id": "Z1234567890ABC",
  "created_resources": [
    "s3_bucket",
    "cloudfront_distribution",
    "acm_certificate",
    "route53_validation_record",
    "route53_alias_record"
  ]
}
```

This file serves two purposes:

- **Redeployment**: On subsequent runs, the script detects existing resources and skips creation. It uploads new files and invalidates the CloudFront cache instead.
- **Teardown**: The `--destroy` flag reads this file to determine exactly which resources to remove.

You should commit this file to version control so the deployment state is shared across your team.

## Cache strategy

The script applies different cache headers based on file type:

| File type | Cache-Control header | Reason |
|-----------|---------------------|--------|
| `.html` | `no-cache` | Browser must always revalidate to pick up new asset references |
| Files under `assets/` | `public, max-age=31536000, immutable` | Vite includes a content hash in the filename, so a given URL never changes |
| Everything else | (no header set) | Uses the default behavior of S3/CloudFront |

## Deployment modes

### S3-only (default)

- The bucket is configured with public access and a static website hosting policy
- `index.html` is set as both the index and error document (for SPA client-side routing)
- Your site is served over HTTP at `http://<bucket>.s3-website-<region>.amazonaws.com`

### CloudFront (`--cloudfront`)

- The bucket stays private — no public access, no website hosting configuration
- CloudFront is the sole accessor via Origin Access Control (OAC)
- HTTPS is enforced, HTTP requests are redirected
- 403 errors are mapped to `/index.html` with a 200 response for SPA routing
- Your site is served at `https://<distribution-id>.cloudfront.net`

### CloudFront + custom domain (`--cloudfront --domain`)

- Everything from CloudFront mode, plus:
- An ACM certificate is provisioned and attached to the distribution
- A Route53 A-record alias maps your domain to CloudFront
- Your site is served at `https://<your-domain>`
