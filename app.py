"""
Ponto de entrada do Streamlit: um único script evita entradas duplicadas no menu.
A UI vive em `historico_atletas.py`.
"""
from historico_atletas import render_page

if __name__ == "__main__":
    render_page()
