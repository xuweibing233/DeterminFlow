use std::time::Duration;

use futures_util::future::join3;
use semver::Version;
use serde::{Deserialize, Serialize};
use tauri::{Manager, ResourceId, Runtime, Webview};
use tauri_plugin_updater::{Update, UpdaterExt};
use url::Url;

// Fork (xuweibing233) secondary-development baseline: all three update channels
// resolve to this fork's GitHub releases. The R2/Gitee slots are kept for the
// original three-source selection logic but point at the fork's channel;
// GiteeRelease's asset parsing (assets[].name + browser_download_url) is
// shape-compatible with the GitHub releases API.
const R2_UPDATE_ENDPOINT: &str =
    "https://github.com/xuweibing233/DeterminFlow/releases/latest/download/latest.json";
const GITHUB_UPDATE_ENDPOINT: &str =
    "https://github.com/xuweibing233/DeterminFlow/releases/latest/download/latest.json";
const GITEE_LATEST_RELEASE_API: &str =
    "https://api.github.com/repos/xuweibing233/DeterminFlow/releases/latest";
const UPDATE_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Deserialize)]
struct GiteeAsset {
    name: String,
    browser_download_url: String,
}

#[derive(Deserialize)]
struct GiteeRelease {
    assets: Vec<GiteeAsset>,
}

struct SelectedUpdates {
    primary: Update,
    fallback: Option<Update>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum UpdateSource {
    R2,
    Github,
    Gitee,
}

#[derive(Debug, Eq, PartialEq)]
struct UpdatePlan {
    primary: UpdateSource,
    fallback: Option<UpdateSource>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateMetadata {
    rid: ResourceId,
    #[serde(skip_serializing_if = "Option::is_none")]
    fallback_rid: Option<ResourceId>,
    current_version: String,
    version: String,
    date: Option<String>,
    body: Option<String>,
    raw_json: serde_json::Value,
}

async fn gitee_update_endpoint() -> Result<Url, String> {
    let client = reqwest::Client::builder()
        .timeout(UPDATE_TIMEOUT)
        .build()
        .map_err(|error| error.to_string())?;
    let release = client
        .get(GITEE_LATEST_RELEASE_API)
        .header(reqwest::header::USER_AGENT, "DeterminFlow-Updater")
        .send()
        .await
        .map_err(|error| error.to_string())?
        .error_for_status()
        .map_err(|error| error.to_string())?
        .json::<GiteeRelease>()
        .await
        .map_err(|error| error.to_string())?;
    let asset = release
        .assets
        .into_iter()
        .find(|asset| asset.name == "latest.json")
        .ok_or_else(|| "Gitee 最新发行版缺少 latest.json".to_string())?;
    Url::parse(&asset.browser_download_url).map_err(|error| error.to_string())
}

async fn check_endpoint<R: Runtime>(
    webview: Webview<R>,
    endpoint: Result<Url, String>,
) -> Result<Option<Update>, String> {
    let endpoint = endpoint?;
    if endpoint.scheme() != "https" {
        return Err("更新地址必须使用 HTTPS".to_string());
    }
    let updater = webview
        .updater_builder()
        .endpoints(vec![endpoint])
        .map_err(|error| error.to_string())?
        .timeout(UPDATE_TIMEOUT)
        .build()
        .map_err(|error| error.to_string())?;
    updater.check().await.map_err(|error| error.to_string())
}

fn plan_update_sources(
    r2: Option<(&str, &str)>,
    github: Option<(&str, &str)>,
    gitee: Option<(&str, &str)>,
) -> Result<Option<UpdatePlan>, String> {
    let parsed = [
        (UpdateSource::R2, r2),
        (UpdateSource::Github, github),
        (UpdateSource::Gitee, gitee),
    ]
    .into_iter()
    .filter_map(|(source, candidate)| candidate.map(|value| (source, value)))
    .filter_map(|(source, (version, signature))| {
        Version::parse(version)
            .ok()
            .map(|parsed_version| (source, parsed_version, signature))
    })
    .collect::<Vec<_>>();
    let authoritative_version = parsed
        .iter()
        .filter(|candidate| candidate.0 != UpdateSource::R2)
        .map(|candidate| &candidate.1)
        .max();
    let latest_version = authoritative_version.or_else(|| {
        parsed
            .iter()
            .filter(|candidate| candidate.0 == UpdateSource::R2)
            .map(|candidate| &candidate.1)
            .max()
    });
    let Some(latest_version) = latest_version else {
        return Ok(None);
    };
    let latest = parsed
        .iter()
        .filter(|candidate| &candidate.1 == latest_version)
        .collect::<Vec<_>>();

    let github = latest
        .iter()
        .find(|candidate| candidate.0 == UpdateSource::Github);
    let gitee = latest
        .iter()
        .find(|candidate| candidate.0 == UpdateSource::Gitee);
    let legacy = match (github, gitee) {
        (Some(github), Some(gitee)) if !gitee.2.is_empty() && github.2 == gitee.2 => {
            Some(UpdatePlan {
                primary: UpdateSource::Gitee,
                fallback: Some(UpdateSource::Github),
            })
        }
        (Some(_), _) => Some(UpdatePlan {
            primary: UpdateSource::Github,
            fallback: None,
        }),
        (None, Some(_)) => Some(UpdatePlan {
            primary: UpdateSource::Gitee,
            fallback: None,
        }),
        (None, None) => None,
    };
    let r2 = latest
        .iter()
        .find(|candidate| candidate.0 == UpdateSource::R2);
    let Some(r2) = r2 else {
        return Ok(legacy);
    };
    let Some(legacy) = legacy else {
        return Ok(Some(UpdatePlan {
            primary: UpdateSource::R2,
            fallback: None,
        }));
    };
    let legacy_signature = match legacy.primary {
        UpdateSource::Github => github.expect("GitHub update plan requires an update").2,
        UpdateSource::Gitee => gitee.expect("Gitee update plan requires an update").2,
        UpdateSource::R2 => unreachable!("legacy plan cannot select R2"),
    };
    if !r2.2.is_empty() && r2.2 == legacy_signature {
        return Ok(Some(UpdatePlan {
            primary: UpdateSource::R2,
            fallback: Some(legacy.primary),
        }));
    }
    Ok(Some(legacy))
}

fn choose_update(
    r2: Result<Option<Update>, String>,
    github: Result<Option<Update>, String>,
    gitee: Result<Option<Update>, String>,
) -> Result<Option<SelectedUpdates>, String> {
    let errors = [r2.as_ref(), github.as_ref(), gitee.as_ref()]
        .into_iter()
        .filter_map(|result| result.err())
        .cloned()
        .collect::<Vec<_>>();
    if errors.len() == 3 {
        return Err(format!(
            "R2、GitHub 与 Gitee 更新源均不可用: {}",
            errors.join("; ")
        ));
    }
    let mut r2 = r2.ok().flatten();
    let mut github = github.ok().flatten();
    let mut gitee = gitee.ok().flatten();

    let plan = plan_update_sources(
        r2.as_ref()
            .map(|update| (update.version.as_str(), update.signature.as_str())),
        github
            .as_ref()
            .map(|update| (update.version.as_str(), update.signature.as_str())),
        gitee
            .as_ref()
            .map(|update| (update.version.as_str(), update.signature.as_str())),
    )?;
    Ok(plan.map(|plan| match plan.primary {
        UpdateSource::R2 => SelectedUpdates {
            primary: r2.take().expect("R2 update plan requires an update"),
            fallback: match plan.fallback {
                Some(UpdateSource::Github) => github.take(),
                Some(UpdateSource::Gitee) => gitee.take(),
                _ => None,
            },
        },
        UpdateSource::Github => SelectedUpdates {
            primary: github
                .take()
                .expect("GitHub update plan requires an update"),
            fallback: match plan.fallback {
                Some(UpdateSource::Gitee) => gitee.take(),
                _ => None,
            },
        },
        UpdateSource::Gitee => SelectedUpdates {
            primary: gitee.take().expect("Gitee update plan requires an update"),
            fallback: match plan.fallback {
                Some(UpdateSource::Github) => github.take(),
                _ => None,
            },
        },
    }))
}

#[cfg(test)]
mod tests {
    use super::{plan_update_sources, UpdatePlan, UpdateSource};

    #[test]
    fn gitee_is_primary_with_github_fallback_for_the_same_signed_release() {
        let plan = plan_update_sources(
            None,
            Some(("1.2.3", "same-signature")),
            Some(("1.2.3", "same-signature")),
        )
        .unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::Gitee,
                fallback: Some(UpdateSource::Github),
            })
        );
    }

    #[test]
    fn newer_release_wins_without_cross_version_fallback() {
        let plan = plan_update_sources(
            None,
            Some(("1.2.4", "github-signature")),
            Some(("1.2.3", "gitee-signature")),
        )
        .unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::Github,
                fallback: None,
            })
        );
    }

    #[test]
    fn signature_mismatch_falls_back_to_the_authoritative_github_release() {
        let plan = plan_update_sources(
            None,
            Some(("1.2.3", "github-signature")),
            Some(("1.2.3", "gitee-signature")),
        )
        .unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::Github,
                fallback: None,
            })
        );
    }

    #[test]
    fn r2_is_primary_with_legacy_fallback_for_the_same_signed_release() {
        let plan = plan_update_sources(
            Some(("1.2.3", "same-signature")),
            Some(("1.2.3", "same-signature")),
            Some(("1.2.3", "same-signature")),
        )
        .unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::R2,
                fallback: Some(UpdateSource::Gitee),
            })
        );
    }

    #[test]
    fn r2_signature_mismatch_keeps_the_legacy_authority() {
        let plan = plan_update_sources(
            Some(("1.2.3", "r2-signature")),
            Some(("1.2.3", "official-signature")),
            Some(("1.2.3", "official-signature")),
        )
        .unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::Gitee,
                fallback: Some(UpdateSource::Github),
            })
        );
    }

    #[test]
    fn r2_does_not_override_a_different_authoritative_version() {
        let plan = plan_update_sources(
            Some(("1.2.4", "r2-signature")),
            Some(("1.2.3", "github-signature")),
            Some(("1.2.3", "gitee-signature")),
        )
        .unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::Github,
                fallback: None,
            })
        );
    }

    #[test]
    fn r2_is_usable_when_legacy_sources_are_unavailable() {
        let plan = plan_update_sources(Some(("1.2.4", "r2-signature")), None, None).unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::R2,
                fallback: None,
            })
        );
    }

    #[test]
    fn invalid_r2_version_does_not_block_an_authoritative_release() {
        let plan = plan_update_sources(
            Some(("not-semver", "r2-signature")),
            Some(("1.2.4", "github-signature")),
            None,
        )
        .unwrap();

        assert_eq!(
            plan,
            Some(UpdatePlan {
                primary: UpdateSource::Github,
                fallback: None,
            })
        );
    }
}

#[tauri::command]
pub async fn check_update_sources<R: Runtime>(
    webview: Webview<R>,
) -> Result<Option<UpdateMetadata>, String> {
    let r2_url = Url::parse(R2_UPDATE_ENDPOINT).map_err(|error| error.to_string());
    let r2 = check_endpoint(webview.clone(), r2_url);
    let github_url = Url::parse(GITHUB_UPDATE_ENDPOINT).map_err(|error| error.to_string());
    let github = check_endpoint(webview.clone(), github_url);

    let gitee = async {
        let endpoint = gitee_update_endpoint().await;
        check_endpoint(webview.clone(), endpoint).await
    };
    let (r2_result, github_result, gitee_result) = join3(r2, github, gitee).await;
    let selected = choose_update(r2_result, github_result, gitee_result)?;

    Ok(selected.map(|selected| {
        let fallback_rid = selected
            .fallback
            .map(|update| webview.resources_table().add(update));
        let update = selected.primary;
        UpdateMetadata {
            current_version: update.current_version.clone(),
            version: update.version.clone(),
            date: update.date.map(|date| date.to_string()),
            body: update.body.clone(),
            raw_json: update.raw_json.clone(),
            rid: webview.resources_table().add(update),
            fallback_rid,
        }
    }))
}
