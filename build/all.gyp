# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

{
  'targets': [
    {
      'target_name': 'All',
      'type': 'none',
      'xcode_create_dependents_test_runner': 1,
      'dependencies': [
        'some.gyp:*',
        '../base/base.gyp:*',
      ],
      'conditions': [
        ['os_posix==1 and OS!="android"', {
          'dependencies': [
          ],
        }],
        ['OS=="mac" or OS=="win"', {
          'dependencies': [
           ],
        }],
        ['OS=="mac"', {
          'dependencies': [
          ],
        }],
        ['OS=="linux"', {
          'dependencies': [
          ],
          'conditions': [
            ['branding=="Chrome"', {
              'dependencies': [
              ],
            }],
          ],
        }],
        ['toolkit_uses_gtk==1', {
          'dependencies': [
            '../tools/gtk_clipboard_dump/gtk_clipboard_dump.gyp:*',
            '../tools/xdisplaycheck/xdisplaycheck.gyp:*',
          ],
        }],
        ['OS=="win"', {
          'conditions': [
            ['win_use_allocator_shim==1', {
              'dependencies': [
                '../base/allocator/allocator.gyp:*',
              ],
            }],
          ],
          'dependencies': [
          ],
        }, {
          'dependencies': [
          ],
        }],
        ['toolkit_views==1', {
          'dependencies': [
          ],
        }],
        ['use_aura==1', {
          'dependencies': [
          ],
        }],
        ['remoting==1', {
          'dependencies': [
          ],
        }],
        ['use_openssl==0', {
          'dependencies': [
          ],
        }],
      ],
    }, # target_name: All
    {
      'target_name': 'All_syzygy',
      'type': 'none',
      'conditions': [
        ['OS=="win" and fastbuild==0', {
            'dependencies': [
            ],
          },
        ],
      ],
    }, # target_name: All_syzygy
    {
      'target_name': 'chromium_builder_tests',
      'type': 'none',
      'dependencies': [
        '../base/base.gyp:base_unittests',
       ],
      'conditions': [
        ['OS=="win"', {
          'dependencies': [
 
           ],
        }],
      ],
    }, # target_name: chromium_builder_tests
    {
      'target_name': 'chromium_2010_builder_tests',
      'type': 'none',
      'dependencies': [
       ],
    }, # target_name: chromium_2010_builder_tests
    {
      'target_name': 'chromium_builder_nacl_win_integration',
      'type': 'none',
      'dependencies': [
      ],
    }, # target_name: chromium_builder_nacl_win_integration
    {
      'target_name': 'chromium_builder_perf',
      'type': 'none',
      'dependencies': [
      ],
    }, # target_name: chromium_builder_perf
    {
      'target_name': 'chromium_gpu_builder',
      'type': 'none',
      'dependencies': [
      ],
    }, # target_name: chromium_gpu_builder
    {
      'target_name': 'chromium_gpu_debug_builder',
      'type': 'none',
      'dependencies': [
      ],
    }, # target_name: chromium_gpu_debug_builder
    {
      'target_name': 'chromium_builder_qa',
      'type': 'none',
      'dependencies': [
      ],
      'conditions': [
        # If you change this condition, make sure you also change it
        # in chrome_tests.gypi
        ['enable_automation==1 and (OS=="mac" or OS=="win" or (os_posix==1 and target_arch==python_arch))', {
          'dependencies': [
          ],
        }],
      ],
    }, # target_name: chromium_builder_qa
  ],
  'conditions': [
    ['OS=="mac"', {
      'targets': [
        {
          # Target to build everything plus the dmg.  We don't put the dmg
          # in the All target because developers really don't need it.
          'target_name': 'all_and_dmg',
          'type': 'none',
          'dependencies': [
            'All',
           ],
        },
        # These targets are here so the build bots can use them to build
        # subsets of a full tree for faster cycle times.
        {
          'target_name': 'chromium_builder_dbg',
          'type': 'none',
          'dependencies': [
 
          ],
        },
        {
          'target_name': 'chromium_builder_rel',
          'type': 'none',
          'dependencies': [
          ],
        },
        {
          'target_name': 'chromium_builder_dbg_tsan_mac',
          'type': 'none',
          'dependencies': [
            '../base/base.gyp:base_unittests',
          ],
        },
        {
          'target_name': 'chromium_builder_dbg_valgrind_mac',
          'type': 'none',
          'dependencies': [
            '../base/base.gyp:base_unittests',
          ],
        },
      ],  # targets
    }], # OS="mac"
    ['OS=="win"', {
      'targets': [
        # These targets are here so the build bots can use them to build
        # subsets of a full tree for faster cycle times.
        {
          'target_name': 'chromium_builder',
          'type': 'none',
          'dependencies': [

          ],
        },
        {
          'target_name': 'chromium_builder_win_cf',
          'type': 'none',
          'dependencies': [
          ],
        },
        {
          'target_name': 'chromium_builder_dbg_tsan_win',
          'type': 'none',
          'dependencies': [
            '../base/base.gyp:base_unittests',
          ],
        },
        {
          'target_name': 'chromium_builder_dbg_drmemory_win',
          'type': 'none',
          'dependencies': [
            '../base/base.gyp:base_unittests',
          ],
        },
        {
          'target_name': 'webkit_builder_win',
          'type': 'none',
          'dependencies': [
          ],
        },
      ],  # targets
      'conditions': [
        ['branding=="Chrome"', {
          'targets': [
            {
              'target_name': 'chrome_official_builder',
              'type': 'none',
              'dependencies': [
              ],
              'conditions': [
                ['internal_pdf', {
                  'dependencies': [

                  ],
                }], # internal_pdf
                ['component != "shared_library" and wix_exists == "True" and \
                    platformsdk_exists == "True"', {
                  'dependencies': [

                  ],
                }], # component != "shared_library"
              ]
            },
          ], # targets
        }], # branding=="Chrome"
       ], # conditions
    }], # OS="win"
    ['use_aura==1', {
      'targets': [
        {
          'target_name': 'aura_builder',
          'type': 'none',
          'dependencies': [
          ],
          'conditions': [
            ['OS=="win"', {
              # Remove this when we have the real compositor.
              'copies': [
                {
                  'destination': '<(PRODUCT_DIR)',
                  'files': ['../third_party/directxsdk/files/dlls/D3DX10d_43.dll']
                },
              ],
              'dependencies': [
              ],
            }],
            ['use_ash==1', {
              'dependencies': [
              ],
            }],
            ['OS=="linux"', {
              # Tests that currently only work on Linux.
              'dependencies': [
                '../base/base.gyp:base_unittests',
              ],
            }],
            ['OS=="mac"', {
              # Exclude dependencies that are not currently implemented.
              'dependencies!': [
              ],
            }],
            ['chromeos==1', {
              'dependencies': [
              ],
            }],
          ],
        },
      ],  # targets
    }], # "use_aura==1"
  ], # conditions
}
