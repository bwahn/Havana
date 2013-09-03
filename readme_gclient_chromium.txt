##############################################################
##### command                                             ####
##############################################################

// 최초 code를 다운로드할때
	> cd E:\chromiumwork\gclient_chromium\

	> gclient config https://src.chromium.org/svn/trunk/src
	> gclient sync

// 디렉토리를 지우고 다시 하고 싶을때
	> cd E:\chromiumwork\gclient_chromium\
	> c:/util/delfolder.bat E:\chromiumwork\gclient_chromium\src
	> gclient sync

// 평상시 최신 코드 동기화할때
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
