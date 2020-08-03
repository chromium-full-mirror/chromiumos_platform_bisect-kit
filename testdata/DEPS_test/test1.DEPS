gclient_gn_args_file = "src/build/config/gclient_args.gni"
gclient_gn_args = ['build_with_chromium', 'checkout_android', 'checkout_android_prebuilts_build_tools', 'checkout_android_native_support', 'checkout_google_benchmark', 'checkout_ios_webkit', 'checkout_nacl', 'checkout_oculus_sdk', 'checkout_openxr']
allowed_hosts = [
  "android.googlesource.com",
  "aomedia.googlesource.com",
  "skia.googlesource.com",
  "swiftshader.googlesource.com",
  "webrtc.googlesource.com",
]
deps = {
  # buildspec_release -> src -> src/tools/luci-go
  "src/tools/luci-go": {
    "packages": [
      {
        "package": "infra/tools/luci/isolate/${{platform}}",
        "version": "git_revision:56ae79476e3caf14da59d75118408aa778637936",
      },
      {
        "package": "infra/tools/luci/isolated/${{platform}}",
        "version": "git_revision:56ae79476e3caf14da59d75118408aa778637936",
      },
      {
        "package": "infra/tools/luci/swarming/${{platform}}",
        "version": "git_revision:56ae79476e3caf14da59d75118408aa778637936",
      },
    ],
    "dep_type": "cipd",
  },
  # buildspec_release -> src -> src/tools/skia_goldctl/linux
  "src/tools/skia_goldctl/linux": {
    "packages": [
      {
        "package": "skia/tools/goldctl/linux-amd64",
        "version": "git_revision:03ea4eb574acd232e223a6b13d15ebfb61f1c0d8",
      },
    ],
    "dep_type": "cipd",
    "condition": 'checkout_linux',
  },
  # buildspec_release -> src -> src/tools/skia_goldctl/mac
  "src/tools/skia_goldctl/mac": {
    "packages": [
      {
        "package": "skia/tools/goldctl/mac-amd64",
        "version": "git_revision:03ea4eb574acd232e223a6b13d15ebfb61f1c0d8",
      },
    ],
    "dep_type": "cipd",
    "condition": 'checkout_mac',
  },
  # buildspec_release -> src -> src/tools/skia_goldctl/win
  "src/tools/skia_goldctl/win": {
    "packages": [
      {
        "package": "skia/tools/goldctl/windows-amd64",
        "version": "git_revision:03ea4eb574acd232e223a6b13d15ebfb61f1c0d8",
      },
    ],
    "dep_type": "cipd",
    "condition": 'checkout_win',
  },
  # buildspec_release -> src -> src/tools/swarming_client
  "src/tools/swarming_client": {
    "url": "https://chromium.googlesource.com/infra/luci/client-py.git@90c5e17a82612bc898c90ab1530dd1bd5822eae8",
  },
  # buildspec_release -> src -> src/v8
  "src/v8": {
    "url": "https://chromium.googlesource.com/v8/v8.git@b28e75d6db7ab366c53586b2b777c84a1876887b",
  },
}
hooks = [
  # buildspec_release -> src
  {
    "name": "disable_depot_tools_selfupdate",
    "pattern": Str('.'),
    "cwd": ".",
    "action": [
        "python",
        "src/third_party/depot_tools/update_depot_tools_toggle.py",
        "--disable",
    ]
  },
  # buildspec_release -> src
  {
    "name": "landmines",
    "pattern": ".",
    "cwd": ".",
    "action": [
        "python",
        "src/build/landmines.py",
    ]
  },
  # buildspec_release -> src
  {
    "name": "remove_stale_pyc_files",
    "pattern": ".",
    "cwd": ".",
    "action": [
        "python",
        "src/tools/remove_stale_pyc_files.py",
        "src/android_webview/tools",
        "src/build/android",
        "src/gpu/gles2_conform_support",
        "src/infra",
        "src/ppapi",
        "src/printing",
        "src/third_party/blink/renderer/build/scripts",
        "src/third_party/blink/tools",
        "src/third_party/catapult",
        "src/tools",
    ]
  },
]
vars = {
  # buildspec_release -> tools_internal
  "webkit_url": 'https://chromium.googlesource.com/chromium/blink.git',
  # buildspec_release -> src
  "webrtc_git": 'https://webrtc.googlesource.com',
  # buildspec_release -> src
  "wuffs_revision": '7ec252876541ec203659949450fafddc148b606e',
}
# https://chrome-internal.googlesource.com/a/chrome/tools/build/internal.DEPS.git@e82706d9cc08295fa19e8f4d1939e80712bc4e90, DEPS
# https://chrome-internal.googlesource.com/chrome/browser/media/kaleidoscope/internal.git@df6b634f536d79e2e3920ccc39bf62e4e2a6bfde, DEPS
# https://chrome-internal.googlesource.com/chrome/ios_internal.git@66a24438afba8b10564e8c14b2a847de0f5f0a9a, DEPS
# https://chrome-internal.googlesource.com/chrome/src-internal.git@1ce9ecabecb1ac3bc4e6b21dc97c5cbc9cfee4ea, DEPS
# https://chrome-internal.googlesource.com/chrome/tools/build_limited/scripts/slave.git@c241c6d04b38802fe78eb095f6e3099532552e3a, DEPS
# https://chrome-internal.googlesource.com/clank/internal/apps.git@93d587af8d813de5966f8018f9b1f8ea26b05f4a, DEPS
# https://chrome-internal.googlesource.com/external/gob/libassistant-internal/standalone/src.git@2d3822fe213fe8ec6b7037142ab267d1170fd745, DEPS
# https://chromium.googlesource.com/a/chromium/src.git@c354f64fc8efb8cfaed803219c3acb334f5f7b61, DEPS
# https://chromium.googlesource.com/android_tools.git@347a7c8078a009e98995985b7ab6ec6b35696dea, DEPS
# https://chromium.googlesource.com/angle/angle.git@209cf8fa408b0a45c3b0577a51c0260971fcfda3, DEPS
# https://chromium.googlesource.com/chromium/src/buildtools.git@aef76d7aad3a178992b5d3d341dc8c25c1e2c0f1, DEPS
# https://chromium.googlesource.com/openscreen@8cce349b0a595ddf7178d5730e980ace3a1d1a53, DEPS
