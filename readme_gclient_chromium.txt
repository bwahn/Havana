##############################################################
##### command                                             ####
##############################################################

// how to get chromium
	> cd E:\chromiumwork\gclient_chromium\

	> gclient config https://src.chromium.org/svn/trunk/src
	> gclient sync

// remove and get code
	> cd E:\chromiumwork\gclient_chromium\
	> c:/util/delfolder.bat E:\chromiumwork\gclient_chromium\src
	> gclient sync

// sync
	> cd E:\chromiumwork\gclient_chromium\
	> gclient sync

##############################################################
##### update history                                      ####
##############################################################
- 2013/01/03
	> cd E:\chromiumwork\gclient_chromium\
	> gclient sync

- 2012/06/27 
	; automated_ui_tests Build failed
	; browser Build failed
