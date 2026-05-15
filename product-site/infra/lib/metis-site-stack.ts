import * as fs from "fs";
import * as path from "path";
import { Construct } from "constructs";
import {
  CfnOutput,
  Duration,
  Fn,
  RemovalPolicy,
  Stack,
  StackProps,
} from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as targets from "aws-cdk-lib/aws-route53-targets";
import * as iam from "aws-cdk-lib/aws-iam";

export interface MetisSiteStackProps extends StackProps {
  siteDomain: string;
  wwwSubdomain: string;
  githubOwner: string;
  githubRepo: string;
  githubBranch: string;
}

export class MetisSiteStack extends Stack {
  constructor(scope: Construct, id: string, props: MetisSiteStackProps) {
    super(scope, id, props);

    const {
      siteDomain,
      wwwSubdomain,
      githubOwner,
      githubRepo,
      githubBranch,
    } = props;

    // ----------------------------------------------------------------
    // DNS: Route 53 hosted zone for the apex domain.
    // After `cdk deploy`, the stack outputs the four NS records; you copy
    // them into the domain's registrar settings to delegate DNS to Route 53.
    // ----------------------------------------------------------------
    const hostedZone = new route53.PublicHostedZone(this, "HostedZone", {
      zoneName: siteDomain,
      comment: `Public DNS for ${siteDomain} (Metis marketing site)`,
    });

    // ----------------------------------------------------------------
    // TLS certificate covering both apex and www. DNS-validated via the
    // hosted zone above. Must be in us-east-1 for CloudFront to use it
    // (the whole stack is pinned there in bin/metis-site.ts).
    // ----------------------------------------------------------------
    const certificate = new acm.Certificate(this, "SiteCertificate", {
      domainName: siteDomain,
      subjectAlternativeNames: [wwwSubdomain],
      validation: acm.CertificateValidation.fromDns(hostedZone),
    });

    // ----------------------------------------------------------------
    // S3 bucket holding the built site. Locked down: no public access,
    // SSE-S3 encryption, versioned so we can roll back a bad deploy.
    // ----------------------------------------------------------------
    const siteBucket = new s3.Bucket(this, "SiteBucket", {
      bucketName: `metis-site-${siteDomain.replace(/\./g, "-")}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          // Drop old object versions after 30 days to keep storage bounded.
          noncurrentVersionExpiration: Duration.days(30),
        },
      ],
    });

    // ----------------------------------------------------------------
    // CloudFront Function: viewer-request handler that does (a) the
    // www → apex 301 and (b) directory-URL rewrites for Astro pages.
    // We read the JS source and substitute the domain tokens at synth.
    // ----------------------------------------------------------------
    const fnSource = fs
      .readFileSync(
        path.join(__dirname, "..", "cloudfront", "viewer-request.js"),
        "utf8",
      )
      .replace(/__APEX_DOMAIN__/g, siteDomain)
      .replace(/__WWW_DOMAIN__/g, wwwSubdomain);

    const viewerRequestFn = new cloudfront.Function(this, "ViewerRequestFn", {
      code: cloudfront.FunctionCode.fromInline(fnSource),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      comment: "www → apex redirect + directory-URL rewrite",
    });

    // ----------------------------------------------------------------
    // CloudFront distribution. OAC (origin-access control) is the modern
    // replacement for OAI — signs requests to S3 with SigV4 so the bucket
    // can stay private. CDK wires the bucket policy automatically when
    // S3BucketOrigin.withOriginAccessControl() is used.
    // ----------------------------------------------------------------
    const distribution = new cloudfront.Distribution(this, "Distribution", {
      domainNames: [siteDomain, wwwSubdomain],
      certificate,
      defaultRootObject: "index.html",
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100, // US + EU only
      enableIpv6: true,
      comment: `Metis marketing site — ${siteDomain}`,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy:
          cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
        compress: true,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        functionAssociations: [
          {
            function: viewerRequestFn,
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
          },
        ],
      },
      errorResponses: [
        // SPA-style fallback so unknown paths render the landing page rather
        // than a raw S3 XML 404. Tweak when adding real routes.
        {
          httpStatus: 404,
          responseHttpStatus: 404,
          responsePagePath: "/index.html",
          ttl: Duration.minutes(5),
        },
      ],
    });

    // ----------------------------------------------------------------
    // Route 53 alias records: apex (A + AAAA) and www (A + AAAA).
    // Both point at the same distribution; the function handles the
    // redirect at the edge.
    // ----------------------------------------------------------------
    const cfTarget = route53.RecordTarget.fromAlias(
      new targets.CloudFrontTarget(distribution),
    );

    new route53.ARecord(this, "ApexAliasA", {
      zone: hostedZone,
      recordName: siteDomain,
      target: cfTarget,
    });
    new route53.AaaaRecord(this, "ApexAliasAAAA", {
      zone: hostedZone,
      recordName: siteDomain,
      target: cfTarget,
    });
    new route53.ARecord(this, "WwwAliasA", {
      zone: hostedZone,
      recordName: wwwSubdomain,
      target: cfTarget,
    });
    new route53.AaaaRecord(this, "WwwAliasAAAA", {
      zone: hostedZone,
      recordName: wwwSubdomain,
      target: cfTarget,
    });

    // ----------------------------------------------------------------
    // GitHub Actions OIDC: federated trust so the workflow can assume
    // a role with no long-lived AWS keys stored in GitHub.
    // ----------------------------------------------------------------
    if (githubOwner === "REPLACE_ME" || githubRepo === "REPLACE_ME") {
      // Synth will succeed but the role won't be useful until cdk.json
      // has the real owner/repo. HOSTING.md walks through this.
    }

    const githubOidc = new iam.OpenIdConnectProvider(this, "GitHubOidc", {
      url: "https://token.actions.githubusercontent.com",
      clientIds: ["sts.amazonaws.com"],
    });

    const deployRole = new iam.Role(this, "DeployRole", {
      roleName: "metis-site-deploy",
      assumedBy: new iam.FederatedPrincipal(
        githubOidc.openIdConnectProviderArn,
        {
          StringEquals: {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          },
          StringLike: {
            "token.actions.githubusercontent.com:sub": `repo:${githubOwner}/${githubRepo}:ref:refs/heads/${githubBranch}`,
          },
        },
        "sts:AssumeRoleWithWebIdentity",
      ),
      description: "Role assumed by GitHub Actions to deploy the Metis site.",
    });

    // Scoped permissions: only the site bucket, only this distribution.
    siteBucket.grantReadWrite(deployRole);
    deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:DeleteObject"],
        resources: [siteBucket.arnForObjects("*")],
      }),
    );
    deployRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["cloudfront:CreateInvalidation"],
        resources: [
          `arn:aws:cloudfront::${this.account}:distribution/${distribution.distributionId}`,
        ],
      }),
    );

    // ----------------------------------------------------------------
    // Outputs consumed by HOSTING.md and the GitHub Actions workflow.
    // ----------------------------------------------------------------
    new CfnOutput(this, "BucketName", {
      value: siteBucket.bucketName,
      description: "S3 bucket the site is synced to.",
      exportName: "MetisSite-BucketName",
    });
    new CfnOutput(this, "DistributionId", {
      value: distribution.distributionId,
      description: "CloudFront distribution ID for cache invalidations.",
      exportName: "MetisSite-DistributionId",
    });
    new CfnOutput(this, "DistributionDomain", {
      value: distribution.distributionDomainName,
      description: "CloudFront default domain (use until DNS propagates).",
    });
    new CfnOutput(this, "DeployRoleArn", {
      value: deployRole.roleArn,
      description: "Role ARN for GitHub Actions OIDC (set as AWS_DEPLOY_ROLE).",
      exportName: "MetisSite-DeployRoleArn",
    });
    new CfnOutput(this, "HostedZoneNameServers", {
      value: Fn.join(",", hostedZone.hostedZoneNameServers ?? []),
      description:
        "Copy these into the Route 53 registrar settings for the domain.",
    });
  }
}
