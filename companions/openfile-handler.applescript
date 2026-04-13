on open location thisURL
	set p to thisURL
	if p starts with "openfile://" then
		set p to text 12 thru -1 of p
	end if
	set decoded to do shell script "/usr/bin/python3 -c 'import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))' " & quoted form of p
	do shell script "/usr/bin/open " & quoted form of decoded
end open location
