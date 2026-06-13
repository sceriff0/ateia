FROM pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime

RUN apt-get update && apt-get install -y git

RUN python -m pip install --upgrade pip

RUN pip install \
    numpy==1.26.4 \
    pandas==2.2.2 \
    tifffile==2023.4.12

RUN python -m pip install -U "imagecodecs[all]"

RUN pip install git+https://github.com/vanvalenlab/cellSAM.git
