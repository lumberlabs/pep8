all : alltest

test :
	python test_pep8.py

doctest :
	python -m doctest pep8.py

nosetest :
	nosetests --verbosity 1 --with-doctest --nocapture pep8 test_pep8

alltest : test doctest

multitest :
	python2.5 test_pep8.py
	python2.6 test_pep8.py
	python2.7 test_pep8.py
	python3.0 test_pep8.py
	python3.1 test_pep8.py
	python3.2 test_pep8.py
	python2.5 -m doctest pep8.py
	python2.6 -m doctest pep8.py
	python2.7 -m doctest pep8.py
	python3.0 -m doctest pep8.py
	python3.1 -m doctest pep8.py
	python3.2 -m doctest pep8.py

