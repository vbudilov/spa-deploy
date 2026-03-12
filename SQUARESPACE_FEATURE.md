# Squarespace/External DNS Support

## Overview

The `--squarespace` flag enables deployment with custom domains using external DNS providers (Squarespace, GoDaddy, Namecheap, etc.) instead of Route53.

## What Changed

### 1. New CLI Flag

```bash
spa-deploy --bucket my-app --cloudfront --domain app.example.com --squarespace
```

The `--squarespace` flag:
- Requires `--domain` and `--cloudfront`
- Skips Route53 hosted zone lookup
- Pauses for manual DNS record creation
- Displays clear instructions for DNS configuration

### 2. Modified Functions

#### `request_acm_certificate()`
- Added `external_dns` parameter (default: `False`)
- When `external_dns=True`:
  - Displays ACM validation CNAME record details
  - Pauses with `input()` for user to add the record
  - Waits up to 6 minutes for certificate validation (vs 3 minutes for Route53)
- When `external_dns=False`:
  - Creates validation record in Route53 automatically (original behavior)

#### `destroy_all()`
- Checks `state.get("external_dns")` flag
- Skips Route53 record deletion when using external DNS
- Displays reminder to manually remove DNS records from external provider

#### `preflight_check()`
- Skips Route53 validation checks when `external_dns=True`
- Prevents false errors for deployments using external DNS

#### `main()`
- Validates that `--squarespace` requires `--domain`
- Routes to appropriate certificate request flow based on flag
- Displays CloudFront CNAME instructions after distribution creation
- Saves `external_dns: true` to state file

### 3. State File Changes

New field in `spa_deploy.json`:
```json
{
  "external_dns": true,
  ...
}
```

This field:
- Persists the DNS mode across redeployments
- Controls Route53 operations in destroy and preflight checks
- Prevents accidental Route53 operations on external DNS deployments

### 4. Documentation Updates

- Added `--squarespace` section to README.md
- Added example usage
- Updated CLI help text with example

## User Flow

### Initial Deployment

1. User runs: `spa-deploy --bucket my-app --cloudfront --domain app.example.com --squarespace`
2. Script requests ACM certificate
3. Script displays:
   ```
   ======================================================================
   ACTION REQUIRED: Add the following CNAME record to your DNS provider
   ======================================================================
   
   Record Type: CNAME
   Name:        _abc123.app.example.com
   Value:       _xyz789.acm-validations.aws.
   
   Note: Remove any trailing dots if your DNS provider doesn't support them.
   ======================================================================
   
   Press Enter once you've added the record and it has propagated...
   ```
4. User adds record to Squarespace DNS
5. User presses Enter
6. Script waits for ACM to validate (up to 6 minutes)
7. Script creates CloudFront distribution
8. Script displays:
   ```
   ======================================================================
   ACTION REQUIRED: Add the following CNAME record to your DNS provider
   ======================================================================
   
   Record Type: CNAME
   Name:        app.example.com
   Value:       d1234abcdef.cloudfront.net
   
   Note: Some DNS providers require you to use '@' or leave the name
         blank for the root domain, or just the subdomain part (e.g., 'app')
         if your domain is 'app.example.com'.
   ======================================================================
   ```
9. User adds CNAME to Squarespace DNS
10. Deployment complete!

### Redeployment

- No flags needed: `spa-deploy`
- Script reads `external_dns: true` from state
- Skips Route53 operations automatically
- Updates files and invalidates CloudFront cache

### Teardown

1. User runs: `spa-deploy --destroy`
2. Script displays:
   ```
   The following resources will be destroyed:
     - CloudFront distribution: E1A2B3C4D5E6F7
     - ACM certificate: arn:aws:acm:...
     - S3 bucket: my-app (all objects will be deleted)
   
   Note: You'll need to manually remove DNS records for app.example.com from your DNS provider.
   ```
3. Script deletes AWS resources
4. User manually removes DNS records from Squarespace

## Benefits

1. **Works with any DNS provider** - Not limited to Route53
2. **Maintains automation** - ACM certificate request and CloudFront setup still automated
3. **Clear instructions** - User knows exactly what to add and where
4. **Safe teardown** - Reminds user to clean up external DNS records
5. **Persistent state** - Redeployments work seamlessly

## Testing Checklist

- [ ] Deploy with `--squarespace` flag
- [ ] Verify ACM validation record display
- [ ] Verify CloudFront CNAME display
- [ ] Redeploy without flags (should read from state)
- [ ] Run `--status` (should show external_dns mode)
- [ ] Run preflight checks (should skip Route53)
- [ ] Run `--destroy` (should show manual DNS reminder)
- [ ] Verify state file contains `external_dns: true`
